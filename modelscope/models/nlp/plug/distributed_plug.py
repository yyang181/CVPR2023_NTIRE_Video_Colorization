import os
from typing import Dict

import torch
import torch.nn.functional as F
from megatron import mpu
from megatron.fp16 import FP16_Module
from megatron.utils import print_rank_0

from modelscope.models import TorchModel
from modelscope.models.base import Tensor
from modelscope.utils.logger import get_logger
from modelscope.utils.nlp.distributed import initialize_distributed
from modelscope.utils.nlp.load_checkpoint import pre_load
from modelscope.utils.torch_utils import set_random_seed_mpu
from . import PlugModel
from .configuration_plug import PlugNLGConfig

logger = get_logger(__name__)


class DistributedPlug(TorchModel):

    def __init__(self, model_dir, rank, **kwargs):
        super().__init__(model_dir, **kwargs)
        self.rank = rank
        self.model_cfg = kwargs
        self.config = PlugNLGConfig.from_pretrained(model_dir)
        initialize_distributed(rank, mpu, kwargs['world_size'],
                               kwargs['model_parallel_size'],
                               kwargs['master_ip'], kwargs['master_port'])
        seed = 0 if 'seed' not in kwargs else kwargs['seed']
        set_random_seed_mpu(seed)
        self.iteration = 0
        self.dist_model = self.initialize_model(path_load_tag='model')

    def initialize_model(self, path_load_tag='model'):
        """Build the model."""
        print_rank_0('Building Plug model. It will take a few minutes ...')
        model = PlugModel(self.config)

        if mpu.get_data_parallel_rank() == 0:
            logger.info(
                ' > number of parameters on model parallel rank {}: {}'.format(
                    mpu.get_model_parallel_rank(),
                    sum([p.nelement() for p in model.parameters()])))

        if self.config.deepspeed and self.config.fp16:
            model.half()

        # GPU allocation.
        model.cuda(torch.cuda.current_device())

        # Fp16 conversion.
        if self.config.fp16:
            model = FP16_Module(model)
            if self.config.fp32_embedding:
                model.module.model.bert.embeddings.word_embeddings.float()
                model.module.model.bert.embeddings.position_embeddings.float()
                model.module.model.bert.embeddings.token_type_embeddings.float(
                )
            if self.config.fp32_tokentypes:
                model.module.model.bert.embeddings.token_type_embeddings.float(
                )
            if self.config.fp32_layernorm:
                for name, _module in model.named_modules():
                    if 'LayerNorm' in name:
                        _module.float()

        load_model = pre_load(mpu, self.model_dir, tag=path_load_tag)
        model_dict = model.module.model.state_dict()
        for key in load_model:
            if key not in model_dict.keys():
                print_rank_0('Skip key: ' + key)
            else:
                print_rank_0('Loading key: ' + key)
        model.module.model.load_state_dict(load_model, strict=False)
        return model

    @staticmethod
    def top_k_logits(logits, top_k=0, top_p=0.0, filter_value=-float('Inf')):
        # This function has been mostly taken from huggingface conversational ai code at
        # https://medium.com/huggingface/how-to-build-a-state-of-the-art-
        # conversational-ai-with-transfer-learning-2d818ac26313

        if top_k > 0:
            # Remove all tokens with a probability less than the last token of the top-k
            indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1,
                                                                      None]
            logits[indices_to_remove] = filter_value

        if top_p > 0.0:
            # convert to 1D
            logits = logits.view(logits.size()[1]).contiguous()
            sorted_logits, sorted_indices = torch.sort(logits, descending=True)
            cumulative_probs = torch.cumsum(
                F.softmax(sorted_logits, dim=-1), dim=-1)

            # Remove tokens with cumulative probability above the threshold
            sorted_indices_to_remove = cumulative_probs > top_p
            # Shift the indices to the right to keep also the first token above the threshold
            sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[
                ..., :-1].clone()
            sorted_indices_to_remove[..., 0] = 0
            indices_to_remove = sorted_indices[sorted_indices_to_remove]
            logits[indices_to_remove] = filter_value
            # going back to 2D
            logits = logits.view(1, -1).contiguous()
        return logits

    def generate(self, input: Dict[str, Tensor], out_length=128, *kwargs):
        device = torch.cuda.current_device()
        batch_size = input['input_ids'].shape[0]
        tokens = input['input_ids'].view(1, -1).contiguous().to(device)
        dec_input_ids = input['dec_input_ids'].to(device)
        attention_mask = input['attention_mask'].to(device)
        self.dist_model.eval()
        with torch.no_grad():
            # Only supports batch_size=1
            all_generate_tokens = []
            generate_tokens = []
            counter = 0
            sequence_output = None
            vocab_size = self.config.original_vocab_size
            sep_token_idx = 102  # index of [SEP] token in BertTokenizer
            while counter < out_length:
                if counter % 128 == 0 and counter != 0:
                    # Sliding window
                    generate_tokens.append(sep_token_idx)
                    start = (tokens == sep_token_idx).nonzero(
                        as_tuple=True)[-1]
                    if start + len(generate_tokens) >= 512:
                        tokens = torch.cat([
                            tokens[:start],
                            torch.cuda.LongTensor(generate_tokens)
                        ], -1)[-512:]
                    else:
                        tokens[0][start:start + len(generate_tokens
                                                    )] = torch.cuda.LongTensor(
                                                        generate_tokens)

                    attention_mask = (tokens != 0)
                    dec_input_ids = input['dec_input_ids'].to(device)
                    generate_tokens = []
                    sequence_output = None

                position_ids = torch.full([batch_size, 1],
                                          len(generate_tokens),
                                          dtype=torch.long,
                                          device=device)
                _, logits, sequence_output = self.dist_model(
                    tokens,
                    None,
                    attention_mask,
                    dec_input_ids,
                    attention_mask,
                    position_ids,
                    is_infer=True,
                    sequence_output=sequence_output,
                    parallel_output=False)
                logits = logits[:, -1, :]
                logits = logits / self.model_cfg['temperature']
                logits = self.top_k_logits(
                    logits,
                    top_k=self.model_cfg['top_k'],
                    top_p=self.model_cfg['top_p'])
                log_probs = F.softmax(logits, dim=-1)
                prev = torch.multinomial(log_probs, num_samples=1)
                prev_token = prev[0].item()
                if prev_token >= vocab_size:
                    prev_token = 100
                    prev[0] = 100
                if prev_token == 102 and len(all_generate_tokens) > int(
                        max(1, out_length) * 0.8):
                    break
                if prev_token == 102:
                    counter += 1
                    continue
                dec_input_ids = torch.cat([dec_input_ids, prev], dim=1)
                generate_tokens.append(prev_token)
                all_generate_tokens.append(prev_token)
                counter += 1

            generate_context = []
            for token in all_generate_tokens:
                if generate_context and generate_context[
                        -1] == 100 and token == 100:
                    continue
                else:
                    generate_context.append(token)
            return {'generate_context': generate_context}
