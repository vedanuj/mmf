# Copyright (c) Facebook, Inc. and its affiliates.

import math
import os
from copy import deepcopy

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.nn import CrossEntropyLoss
from transformers.file_utils import PYTORCH_PRETRAINED_BERT_CACHE
from transformers.modeling_bert import (
    ACT2FN,
    BertConfig,
    BertEmbeddings,
    BertIntermediate,
    BertLayerNorm,
    BertLMPredictionHead,
    BertOutput,
    BertPredictionHeadTransform,
    BertPreTrainedModel,
    BertSelfOutput,
)

from pythia.common.registry import registry
from pythia.models import BaseModel


class BertSelfAttention(nn.Module):
    def __init__(self, config):
        super(BertSelfAttention, self).__init__()
        if config.hidden_size % config.num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.hidden_size, config.num_attention_heads)
            )
        self.num_attention_heads = config.num_attention_heads
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.visualization = config.visualization

        self.query = nn.Linear(config.hidden_size, self.all_head_size)
        self.key = nn.Linear(config.hidden_size, self.all_head_size)
        self.value = nn.Linear(config.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (
            self.num_attention_heads,
            self.attention_head_size,
        )
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(self, hidden_states, attention_mask):
        mixed_query_layer = self.query(hidden_states)
        mixed_key_layer = self.key(hidden_states)
        mixed_value_layer = self.value(hidden_states)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
        attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        if self.visualization:
            attn_data = {
                "attn": attention_probs,
                "queries": query_layer,
                "keys": key_layer,
            }
        else:
            attn_data = None

        return context_layer, attn_data


class BertAttention(nn.Module):
    def __init__(self, config):
        super(BertAttention, self).__init__()
        self.self = BertSelfAttention(config)
        self.output = BertSelfOutput(config)

    def forward(self, input_tensor, attention_mask):
        self_output, attention_probs = self.self(input_tensor, attention_mask)
        attention_output = self.output(self_output, input_tensor)
        return attention_output, attention_probs


class BertLayer(nn.Module):
    def __init__(self, config):
        super(BertLayer, self).__init__()
        self.attention = BertAttention(config)
        self.intermediate = BertIntermediate(config)
        self.output = BertOutput(config)

    def forward(self, hidden_states, attention_mask):
        attention_output, attention_probs = self.attention(
            hidden_states, attention_mask
        )
        intermediate_output = self.intermediate(attention_output)
        layer_output = self.output(intermediate_output, attention_output)
        return layer_output, attention_probs


class BertImageSelfAttention(nn.Module):
    def __init__(self, config):
        super(BertImageSelfAttention, self).__init__()
        if config.v_hidden_size % config.v_num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.v_hidden_size, config.v_num_attention_heads)
            )
        self.dynamic_attention = config.dynamic_attention
        self.num_attention_heads = config.v_num_attention_heads
        self.attention_head_size = int(
            config.v_hidden_size / config.v_num_attention_heads
        )

        self.visualization = config.visualization

        self.all_head_size = self.num_attention_heads * self.attention_head_size
        self.query = nn.Linear(config.v_hidden_size, self.all_head_size)
        self.key = nn.Linear(config.v_hidden_size, self.all_head_size)
        self.value = nn.Linear(config.v_hidden_size, self.all_head_size)

        if self.dynamic_attention:
            self.dyLinear_q = nn.Linear(config.hidden_size, self.all_head_size)
            self.dyLinear_k = nn.Linear(config.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.v_attention_probs_dropout_prob)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (
            self.num_attention_heads,
            self.attention_head_size,
        )
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(self, hidden_states, attention_mask, txt_embedding, txt_attention_mask):

        mixed_query_layer = self.query(hidden_states)
        mixed_key_layer = self.key(hidden_states)
        mixed_value_layer = self.value(hidden_states)

        if self.dynamic_attention:
            pool_embedding = (txt_embedding * txt_attention_mask).sum(1)
            pool_embedding = pool_embedding / txt_attention_mask.sum(1)

            # given pool embedding, Linear and Sigmoid layer.
            gate_q = 1 + torch.sigmoid(self.dyLinear_q(pool_embedding))
            gate_k = 1 + torch.sigmoid(self.dyLinear_k(pool_embedding))

            mixed_query_layer = mixed_query_layer * gate_q.unsqueeze(1)
            mixed_key_layer = mixed_key_layer * gate_k.unsqueeze(1)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
        attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        if self.visualization:
            attn_data = {
                "attn": attention_probs,
                "queries": query_layer,
                "keys": key_layer,
            }
        else:
            attn_data = None

        return context_layer, attn_data


class BertImageSelfOutput(nn.Module):
    def __init__(self, config):
        super(BertImageSelfOutput, self).__init__()
        self.dense = nn.Linear(config.v_hidden_size, config.v_hidden_size)
        self.LayerNorm = BertLayerNorm(config.v_hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.v_hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class BertImageAttention(nn.Module):
    def __init__(self, config):
        super(BertImageAttention, self).__init__()
        self.self = BertImageSelfAttention(config)
        self.output = BertImageSelfOutput(config)

    def forward(self, input_tensor, attention_mask, txt_embedding, txt_attention_mask):
        self_output, attention_probs = self.self(
            input_tensor, attention_mask, txt_embedding, txt_attention_mask
        )
        attention_output = self.output(self_output, input_tensor)
        return attention_output, attention_probs


class BertImageIntermediate(nn.Module):
    def __init__(self, config):
        super(BertImageIntermediate, self).__init__()
        self.dense = nn.Linear(config.v_hidden_size, config.v_intermediate_size)
        if isinstance(config.v_hidden_act, str):
            self.intermediate_act_fn = ACT2FN[config.v_hidden_act]
        else:
            self.intermediate_act_fn = config.v_hidden_act

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


class BertImageOutput(nn.Module):
    def __init__(self, config):
        super(BertImageOutput, self).__init__()
        self.dense = nn.Linear(config.v_intermediate_size, config.v_hidden_size)
        self.LayerNorm = BertLayerNorm(config.v_hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.v_hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class BertImageLayer(nn.Module):
    def __init__(self, config):
        super(BertImageLayer, self).__init__()
        self.attention = BertImageAttention(config)
        self.intermediate = BertImageIntermediate(config)
        self.output = BertImageOutput(config)

    def forward(self, hidden_states, attention_mask, txt_embedding, txt_attention_mask):
        attention_output, attention_probs = self.attention(
            hidden_states, attention_mask, txt_embedding, txt_attention_mask
        )
        intermediate_output = self.intermediate(attention_output)
        layer_output = self.output(intermediate_output, attention_output)
        return layer_output, attention_probs


class BertBiAttention(nn.Module):
    def __init__(self, config):
        super(BertBiAttention, self).__init__()
        if config.bi_hidden_size % config.bi_num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.bi_hidden_size, config.bi_num_attention_heads)
            )

        self.visualization = config.visualization
        self.num_attention_heads = config.bi_num_attention_heads
        self.attention_head_size = int(
            config.bi_hidden_size / config.bi_num_attention_heads
        )
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        # self.scale = nn.Linear(1, self.num_attention_heads, bias=False)
        # self.scale_act_fn = ACT2FN['relu']

        self.query1 = nn.Linear(config.v_hidden_size, self.all_head_size)
        self.key1 = nn.Linear(config.v_hidden_size, self.all_head_size)
        self.value1 = nn.Linear(config.v_hidden_size, self.all_head_size)
        # self.logit1 = nn.Linear(config.hidden_size, self.num_attention_heads)

        self.dropout1 = nn.Dropout(config.v_attention_probs_dropout_prob)

        self.query2 = nn.Linear(config.hidden_size, self.all_head_size)
        self.key2 = nn.Linear(config.hidden_size, self.all_head_size)
        self.value2 = nn.Linear(config.hidden_size, self.all_head_size)
        # self.logit2 = nn.Linear(config.hidden_size, self.num_attention_heads)

        self.dropout2 = nn.Dropout(config.attention_probs_dropout_prob)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (
            self.num_attention_heads,
            self.attention_head_size,
        )
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(
        self,
        input_tensor1,
        attention_mask1,
        input_tensor2,
        attention_mask2,
        co_attention_mask=None,
        use_co_attention_mask=False,
    ):

        # for vision input.
        mixed_query_layer1 = self.query1(input_tensor1)
        mixed_key_layer1 = self.key1(input_tensor1)
        mixed_value_layer1 = self.value1(input_tensor1)
        # mixed_logit_layer1 = self.logit1(input_tensor1)

        query_layer1 = self.transpose_for_scores(mixed_query_layer1)
        key_layer1 = self.transpose_for_scores(mixed_key_layer1)
        value_layer1 = self.transpose_for_scores(mixed_value_layer1)
        # logit_layer1 = self.transpose_for_logits(mixed_logit_layer1)

        # for text input:
        mixed_query_layer2 = self.query2(input_tensor2)
        mixed_key_layer2 = self.key2(input_tensor2)
        mixed_value_layer2 = self.value2(input_tensor2)
        # mixed_logit_layer2 = self.logit2(input_tensor2)

        query_layer2 = self.transpose_for_scores(mixed_query_layer2)
        key_layer2 = self.transpose_for_scores(mixed_key_layer2)
        value_layer2 = self.transpose_for_scores(mixed_value_layer2)
        # logit_layer2 = self.transpose_for_logits(mixed_logit_layer2)

        # Take the dot product between "query2" and "key1" to get the raw attention scores for value 1.
        attention_scores1 = torch.matmul(query_layer2, key_layer1.transpose(-1, -2))
        attention_scores1 = attention_scores1 / math.sqrt(self.attention_head_size)
        attention_scores1 = attention_scores1 + attention_mask1
        # if use_co_attention_mask:
        # attention_scores1 = attention_scores1 + co_attention_mask.permute(0,1,3,2)

        # Normalize the attention scores to probabilities.
        attention_probs1 = nn.Softmax(dim=-1)(attention_scores1)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs1 = self.dropout1(attention_probs1)

        context_layer1 = torch.matmul(attention_probs1, value_layer1)
        context_layer1 = context_layer1.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape1 = context_layer1.size()[:-2] + (self.all_head_size,)
        context_layer1 = context_layer1.view(*new_context_layer_shape1)

        # Take the dot product between "query1" and "key2" to get the raw attention scores for value 2.
        attention_scores2 = torch.matmul(query_layer1, key_layer2.transpose(-1, -2))
        attention_scores2 = attention_scores2 / math.sqrt(self.attention_head_size)
        # Apply the attention mask is (precomputed for all layers in BertModel forward() function)

        # we can comment this line for single flow.
        attention_scores2 = attention_scores2 + attention_mask2
        # if use_co_attention_mask:
        # attention_scores2 = attention_scores2 + co_attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs2 = nn.Softmax(dim=-1)(attention_scores2)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs2 = self.dropout2(attention_probs2)

        context_layer2 = torch.matmul(attention_probs2, value_layer2)
        context_layer2 = context_layer2.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape2 = context_layer2.size()[:-2] + (self.all_head_size,)
        context_layer2 = context_layer2.view(*new_context_layer_shape2)

        attn_data = None

        if self.visualization:
            attn_data = {
                "attn1": attention_probs1,
                "queries1": query_layer2,
                "keys1": key_layer1,
                "attn2": attention_probs2,
                "querues2": query_layer1,
                "keys2": key_layer2,
            }

        return context_layer1, context_layer2, attn_data


class BertBiOutput(nn.Module):
    def __init__(self, config):
        super(BertBiOutput, self).__init__()

        self.dense1 = nn.Linear(config.bi_hidden_size, config.v_hidden_size)
        self.LayerNorm1 = BertLayerNorm(config.v_hidden_size, eps=1e-12)
        self.dropout1 = nn.Dropout(config.v_hidden_dropout_prob)

        self.q_dense1 = nn.Linear(config.bi_hidden_size, config.v_hidden_size)
        self.q_dropout1 = nn.Dropout(config.v_hidden_dropout_prob)

        self.dense2 = nn.Linear(config.bi_hidden_size, config.hidden_size)
        self.LayerNorm2 = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.dropout2 = nn.Dropout(config.hidden_dropout_prob)

        self.q_dense2 = nn.Linear(config.bi_hidden_size, config.hidden_size)
        self.q_dropout2 = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states1, input_tensor1, hidden_states2, input_tensor2):

        context_state1 = self.dense1(hidden_states1)
        context_state1 = self.dropout1(context_state1)

        context_state2 = self.dense2(hidden_states2)
        context_state2 = self.dropout2(context_state2)

        hidden_states1 = self.LayerNorm1(context_state1 + input_tensor1)
        hidden_states2 = self.LayerNorm2(context_state2 + input_tensor2)

        return hidden_states1, hidden_states2


class BertConnectionLayer(nn.Module):
    def __init__(self, config):
        super(BertConnectionLayer, self).__init__()
        self.biattention = BertBiAttention(config)

        self.biOutput = BertBiOutput(config)

        self.v_intermediate = BertImageIntermediate(config)
        self.v_output = BertImageOutput(config)

        self.t_intermediate = BertIntermediate(config)
        self.t_output = BertOutput(config)

    def forward(
        self,
        input_tensor1,
        attention_mask1,
        input_tensor2,
        attention_mask2,
        co_attention_mask=None,
        use_co_attention_mask=False,
    ):

        bi_output1, bi_output2, co_attention_probs = self.biattention(
            input_tensor1,
            attention_mask1,
            input_tensor2,
            attention_mask2,
            co_attention_mask,
            use_co_attention_mask,
        )

        attention_output1, attention_output2 = self.biOutput(
            bi_output2, input_tensor1, bi_output1, input_tensor2
        )

        intermediate_output1 = self.v_intermediate(attention_output1)
        layer_output1 = self.v_output(intermediate_output1, attention_output1)

        intermediate_output2 = self.t_intermediate(attention_output2)
        layer_output2 = self.t_output(intermediate_output2, attention_output2)

        return layer_output1, layer_output2, co_attention_probs


class BertEncoder(nn.Module):
    def __init__(self, config):
        super(BertEncoder, self).__init__()

        # in the bert encoder, we need to extract three things here.
        # text bert layer: BertLayer
        # vision bert layer: BertImageLayer
        # Bi-Attention: Given the output of two bertlayer, perform bi-directional
        # attention and add on two layers.

        self.FAST_MODE = config.fast_mode
        self.with_coattention = config.with_coattention
        self.v_biattention_id = config.v_biattention_id
        self.t_biattention_id = config.t_biattention_id
        self.in_batch_pairs = config.in_batch_pairs
        self.fixed_t_layer = config.fixed_t_layer
        self.fixed_v_layer = config.fixed_v_layer
        layer = BertLayer(config)
        v_layer = BertImageLayer(config)
        connect_layer = BertConnectionLayer(config)

        self.layer = nn.ModuleList(
            [deepcopy(layer) for _ in range(config.num_hidden_layers)]
        )
        self.v_layer = nn.ModuleList(
            [deepcopy(v_layer) for _ in range(config.v_num_hidden_layers)]
        )
        self.c_layer = nn.ModuleList(
            [deepcopy(connect_layer) for _ in range(len(config.v_biattention_id))]
        )

    def forward(
        self,
        txt_embedding,
        image_embedding,
        txt_attention_mask,
        txt_attention_mask2,
        image_attention_mask,
        co_attention_mask=None,
        output_all_encoded_layers=True,
        output_all_attention_masks=False,
    ):

        v_start = 0
        t_start = 0
        count = 0
        all_encoder_layers_t = []
        all_encoder_layers_v = []

        all_attention_mask_t = []
        all_attnetion_mask_v = []
        all_attention_mask_c = []

        batch_size, num_words, t_hidden_size = txt_embedding.size()
        _, num_regions, v_hidden_size = image_embedding.size()

        use_co_attention_mask = False
        for v_layer_id, t_layer_id in zip(self.v_biattention_id, self.t_biattention_id):

            v_end = v_layer_id
            t_end = t_layer_id

            assert self.fixed_t_layer <= t_end
            assert self.fixed_v_layer <= v_end

            for idx in range(t_start, self.fixed_t_layer):
                with torch.no_grad():
                    txt_embedding, txt_attention_probs = self.layer[idx](
                        txt_embedding, txt_attention_mask
                    )
                    t_start = self.fixed_t_layer
                    if output_all_attention_masks:
                        all_attention_mask_t.append(txt_attention_probs)

            for idx in range(t_start, t_end):
                txt_embedding, txt_attention_probs = self.layer[idx](
                    txt_embedding, txt_attention_mask
                )
                if output_all_attention_masks:
                    all_attention_mask_t.append(txt_attention_probs)

            for idx in range(v_start, self.fixed_v_layer):
                with torch.no_grad():
                    image_embedding, image_attention_probs = self.v_layer[idx](
                        image_embedding,
                        image_attention_mask,
                        txt_embedding,
                        txt_attention_mask2,
                    )
                    v_start = self.fixed_v_layer

                    if output_all_attention_masks:
                        all_attnetion_mask_v.append(image_attention_probs)

            for idx in range(v_start, v_end):
                image_embedding, image_attention_probs = self.v_layer[idx](
                    image_embedding,
                    image_attention_mask,
                    txt_embedding,
                    txt_attention_mask2,
                )

                if output_all_attention_masks:
                    all_attnetion_mask_v.append(image_attention_probs)

            if count == 0 and self.in_batch_pairs:
                # new batch size is the batch_size ^2
                image_embedding = (
                    image_embedding.unsqueeze(0)
                    .expand(batch_size, batch_size, num_regions, v_hidden_size)
                    .contiguous()
                    .view(batch_size * batch_size, num_regions, v_hidden_size)
                )
                image_attention_mask = (
                    image_attention_mask.unsqueeze(0)
                    .expand(batch_size, batch_size, 1, 1, num_regions)
                    .contiguous()
                    .view(batch_size * batch_size, 1, 1, num_regions)
                )

                txt_embedding = (
                    txt_embedding.unsqueeze(1)
                    .expand(batch_size, batch_size, num_words, t_hidden_size)
                    .contiguous()
                    .view(batch_size * batch_size, num_words, t_hidden_size)
                )
                txt_attention_mask = (
                    txt_attention_mask.unsqueeze(1)
                    .expand(batch_size, batch_size, 1, 1, num_words)
                    .contiguous()
                    .view(batch_size * batch_size, 1, 1, num_words)
                )
                co_attention_mask = (
                    co_attention_mask.unsqueeze(1)
                    .expand(batch_size, batch_size, 1, num_regions, num_words)
                    .contiguous()
                    .view(batch_size * batch_size, 1, num_regions, num_words)
                )

            if count == 0 and self.FAST_MODE:
                txt_embedding = txt_embedding.expand(
                    image_embedding.size(0),
                    txt_embedding.size(1),
                    txt_embedding.size(2),
                )
                txt_attention_mask = txt_attention_mask.expand(
                    image_embedding.size(0),
                    txt_attention_mask.size(1),
                    txt_attention_mask.size(2),
                    txt_attention_mask.size(3),
                )

            if self.with_coattention:
                # do the bi attention.
                image_embedding, txt_embedding, co_attention_probs = self.c_layer[
                    count
                ](
                    image_embedding,
                    image_attention_mask,
                    txt_embedding,
                    txt_attention_mask,
                    co_attention_mask,
                    use_co_attention_mask,
                )

                if output_all_attention_masks:
                    all_attention_mask_c.append(co_attention_probs)

            v_start = v_end
            t_start = t_end
            count += 1

            if output_all_encoded_layers:
                all_encoder_layers_t.append(txt_embedding)
                all_encoder_layers_v.append(image_embedding)

        for idx in range(v_start, len(self.v_layer)):
            image_embedding, image_attention_probs = self.v_layer[idx](
                image_embedding,
                image_attention_mask,
                txt_embedding,
                txt_attention_mask2,
            )

            if output_all_attention_masks:
                all_attnetion_mask_v.append(image_attention_probs)

        for idx in range(t_start, len(self.layer)):
            txt_embedding, txt_attention_probs = self.layer[idx](
                txt_embedding, txt_attention_mask
            )

            if output_all_attention_masks:
                all_attention_mask_t.append(txt_attention_probs)

        # add the end part to finish.
        if not output_all_encoded_layers:
            all_encoder_layers_t.append(txt_embedding)
            all_encoder_layers_v.append(image_embedding)

        return (
            all_encoder_layers_t,
            all_encoder_layers_v,
            (all_attention_mask_t, all_attnetion_mask_v, all_attention_mask_c),
        )


class BertTextPooler(nn.Module):
    def __init__(self, config):
        super(BertTextPooler, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.bi_hidden_size)
        self.activation = nn.ReLU()

    def forward(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        pooled_output = self.activation(pooled_output)
        return pooled_output


class BertImagePooler(nn.Module):
    def __init__(self, config):
        super(BertImagePooler, self).__init__()
        self.dense = nn.Linear(config.v_hidden_size, config.bi_hidden_size)
        self.activation = nn.ReLU()

    def forward(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        pooled_output = self.activation(pooled_output)
        return pooled_output


class BertImgPredictionHeadTransform(nn.Module):
    def __init__(self, config):
        super(BertImgPredictionHeadTransform, self).__init__()
        self.dense = nn.Linear(config.v_hidden_size, config.v_hidden_size)
        if isinstance(config.hidden_act, str):
            self.transform_act_fn = ACT2FN[config.hidden_act]
        else:
            self.transform_act_fn = config.v_hidden_act
        self.LayerNorm = BertLayerNorm(config.v_hidden_size, eps=1e-12)

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.transform_act_fn(hidden_states)
        hidden_states = self.LayerNorm(hidden_states)
        return hidden_states


class BertImagePredictionHead(nn.Module):
    def __init__(self, config):
        super(BertImagePredictionHead, self).__init__()
        self.transform = BertImgPredictionHeadTransform(config)

        # The output weights are the same as the input embeddings, but there is
        # an output-only bias for each token.
        self.decoder = nn.Linear(config.v_hidden_size, config.v_target_size)

    def forward(self, hidden_states):
        hidden_states = self.transform(hidden_states)
        hidden_states = self.decoder(hidden_states)
        return hidden_states


class BertPreTrainingHeads(nn.Module):
    def __init__(self, config):
        super(BertPreTrainingHeads, self).__init__()
        self.predictions = BertLMPredictionHead(config)
        self.bi_seq_relationship = nn.Linear(config.bi_hidden_size, 2)
        self.imagePredictions = BertImagePredictionHead(config)
        self.fusion_method = config.fusion_method
        self.dropout = nn.Dropout(0.1)

    def forward(
        self, sequence_output_t, sequence_output_v, pooled_output_t, pooled_output_v
    ):

        if self.fusion_method == "sum":
            pooled_output = self.dropout(pooled_output_t + pooled_output_v)
        elif self.fusion_method == "mul":
            pooled_output = self.dropout(pooled_output_t * pooled_output_v)
        else:
            assert False

        prediction_scores_t = self.predictions(sequence_output_t)
        seq_relationship_score = self.bi_seq_relationship(pooled_output)
        prediction_scores_v = self.imagePredictions(sequence_output_v)

        return prediction_scores_t, prediction_scores_v, seq_relationship_score


class BertModel(BertPreTrainedModel):
    def __init__(self, config):
        super(BertModel, self).__init__(config)

        # initilize word embedding
        print(config)
        self.embeddings = BertEmbeddings(config)

        self.task_specific_tokens = config.task_specific_tokens

        # initlize the vision embedding
        self.v_embeddings = BertImageEmbeddings(config)

        self.encoder = BertEncoder(config)
        self.t_pooler = BertTextPooler(config)
        self.v_pooler = BertImagePooler(config)

        self.init_weights()

    def forward(
        self,
        input_txt,
        input_imgs,
        image_loc,
        token_type_ids=None,
        attention_mask=None,
        image_attention_mask=None,
        co_attention_mask=None,
        task_ids=None,
        output_all_encoded_layers=False,
        output_all_attention_masks=False,
    ):
        if attention_mask is None:
            attention_mask = torch.ones_like(input_txt)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_txt)
        if image_attention_mask is None:
            image_attention_mask = torch.ones(
                input_imgs.size(0), input_imgs.size(1)
            ).type_as(input_txt)

        if self.task_specific_tokens:
            # extend the mask
            mask_tokens = input_txt.new().resize_(input_txt.size(0), 1).fill_(1)
            attention_mask = torch.cat([mask_tokens, attention_mask], dim=1)

        # We create a 3D attention mask from a 2D tensor mask.
        # Sizes are [batch_size, 1, 1, to_seq_length]
        # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
        # this attention mask is more simple than the triangular masking of causal attention
        # used in OpenAI GPT, we just need to prepare the broadcast dimension here.
        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)
        extended_image_attention_mask = image_attention_mask.unsqueeze(1).unsqueeze(2)

        extended_attention_mask2 = attention_mask.unsqueeze(2)
        # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
        # masked positions, this operation will create a tensor which is 0.0 for
        # positions we want to attend and -10000.0 for masked positions.
        # Since we are adding it to the raw scores before the softmax, this is
        # effectively the same as removing these entirely.
        extended_attention_mask = extended_attention_mask.to(
            dtype=next(self.parameters()).dtype
        )  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

        extended_attention_mask2 = extended_attention_mask2.to(
            dtype=next(self.parameters()).dtype
        )  # fp16 compatibility

        extended_image_attention_mask = extended_image_attention_mask.to(
            dtype=next(self.parameters()).dtype
        )  # fp16 compatibility
        extended_image_attention_mask = (1.0 - extended_image_attention_mask) * -10000.0

        if co_attention_mask is None:
            co_attention_mask = torch.zeros(
                input_txt.size(0), input_imgs.size(1), input_txt.size(1)
            ).type_as(extended_image_attention_mask)

        extended_co_attention_mask = co_attention_mask.unsqueeze(1)

        # extended_co_attention_mask = co_attention_mask.unsqueeze(-1)
        extended_co_attention_mask = extended_co_attention_mask * 5.0
        extended_co_attention_mask = extended_co_attention_mask.to(
            dtype=next(self.parameters()).dtype
        )  # fp16 compatibility

        embedding_output = self.embeddings(input_txt, token_type_ids, task_ids)
        v_embedding_output = self.v_embeddings(input_imgs, image_loc)
        encoded_layers_t, encoded_layers_v, all_attention_mask = self.encoder(
            embedding_output,
            v_embedding_output,
            extended_attention_mask,
            extended_attention_mask2,
            extended_image_attention_mask,
            extended_co_attention_mask,
            output_all_encoded_layers=output_all_encoded_layers,
            output_all_attention_masks=output_all_attention_masks,
        )

        sequence_output_t = encoded_layers_t[-1]
        sequence_output_v = encoded_layers_v[-1]

        pooled_output_t = self.t_pooler(sequence_output_t)
        pooled_output_v = self.v_pooler(sequence_output_v)

        if not output_all_encoded_layers:
            encoded_layers_t = encoded_layers_t[-1]
            encoded_layers_v = encoded_layers_v[-1]

        return (
            encoded_layers_t,
            encoded_layers_v,
            pooled_output_t,
            pooled_output_v,
            all_attention_mask,
        )


class BertImageEmbeddings(nn.Module):
    """Construct the embeddings from image, spatial location (omit now) and token_type embeddings.
    """

    def __init__(self, config):
        super(BertImageEmbeddings, self).__init__()

        self.image_embeddings = nn.Linear(config.v_feature_size, config.v_hidden_size)
        self.image_location_embeddings = nn.Linear(5, config.v_hidden_size)
        self.LayerNorm = BertLayerNorm(config.v_hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, input_ids, input_loc):

        img_embeddings = self.image_embeddings(input_ids)
        loc_embeddings = self.image_location_embeddings(input_loc)

        # TODO: we want to make the padding_idx == 0, however, with custom initilization, it seems it will have a bias.
        # Let's do masking for now
        embeddings = self.LayerNorm(img_embeddings + loc_embeddings)
        embeddings = self.dropout(embeddings)

        return embeddings


class BertForMultiModalPreTraining(BertPreTrainedModel):
    """BERT model with multi modal pre-training heads.
    """

    def __init__(self, config, training_head_type, dropout_prob=0.1):
        super(BertForMultiModalPreTraining, self).__init__(config)

        self.bert = BertModel(config)
        self.cls = BertPreTrainingHeads(config)
        self.training_head_type = training_head_type
        self.fusion_method = config.fusion_method
        self.dropout = nn.Dropout(dropout_prob)
        # config.defrost()
        hidden_size = config.hidden_size
        config.hidden_size = config.bi_hidden_size

        if "vqa" in self.training_head_type:
            self.answer_space_size = 3129
            self.classifier = nn.Sequential(
                BertPredictionHeadTransform(config), nn.Linear(config.hidden_size, 3129)
            )
            # self.classifier = SimpleClassifier(config.bi_hidden_size, config.bi_hidden_size*2, self.answer_space_size, 0.5)
        elif "vizwiz" in self.training_head_type:
            self.answer_space_size = 7371
            self.classifier = nn.Sequential(
                BertPredictionHeadTransform(config), nn.Linear(config.hidden_size, 7371)
            )
            # self.classifier = SimpleClassifier(config.bi_hidden_size, config.bi_hidden_size*2, self.answer_space_size, 0.5)
        elif self.training_head_type == "nlvr2":
            config.hidden_size *= 2
            self.classifier = nn.Sequential(
                BertPredictionHeadTransform(config), nn.Linear(config.hidden_size, 2)
            )
            config.hidden_size /= 2
            # self.classifier = SimpleClassifier(config.bi_hidden_size * 2, config.bi_hidden_size*2, 2, 0.5)
        elif self.training_head_type == "visual_entailment":
            # self.classifier = nn.Linear(config.bi_hidden_size, 3)
            self.classifier = nn.Sequential(
                BertPredictionHeadTransform(config), nn.Linear(config.hidden_size, 3)
            )
        elif self.training_head_type == "mmimdb":
            self.classifier = nn.Sequential(
                BertPredictionHeadTransform(config), nn.Linear(config.hidden_size, 24)
            )

        config.hidden_size = hidden_size
        # config.freeze()
        self.init_weights()
        self.visual_target = config.visual_target
        self.num_negative = config.num_negative
        self.loss_fct = CrossEntropyLoss(ignore_index=-1)

        if self.visual_target == 0:
            self.vis_criterion = nn.KLDivLoss(reduction="none")
        elif self.visual_target == 1:
            self.vis_criterion = nn.MSELoss(reduction="none")
        elif self.visual_target == 2:
            self.vis_criterion = CrossEntropyLoss()

        self.tie_weights()

    def tie_weights(self):
        """ Make sure we are sharing the input and output embeddings.
            Export to TorchScript can't handle parameter sharing so we are cloning them instead.
        """
        self._tie_or_clone_weights(
            self.cls.predictions.decoder, self.bert.embeddings.word_embeddings
        )

    def forward(
        self,
        input_ids,
        image_feat,
        image_loc,
        token_type_ids=None,
        attention_mask=None,
        image_attention_mask=None,
        masked_lm_labels=None,
        image_label=None,
        image_target=None,
        next_sentence_label=None,
        output_all_attention_masks=False,
    ):
        # in this model, we first embed the images.
        sequence_output_t, sequence_output_v, pooled_output_t, pooled_output_v, all_attention_mask = self.bert(
            input_ids,
            image_feat,
            image_loc,
            token_type_ids,
            attention_mask,
            image_attention_mask,
            output_all_encoded_layers=False,
            output_all_attention_masks=output_all_attention_masks,
        )

        prediction_scores_t, prediction_scores_v, seq_relationship_score = self.cls(
            sequence_output_t, sequence_output_v, pooled_output_t, pooled_output_v
        )

        output_dict = {}
        if "pretraining" in self.training_head_type:
            if image_target is not None:
                if self.visual_target == 1:
                    img_loss = self.vis_criterion(prediction_scores_v, image_target)
                    masked_img_loss = torch.sum(
                        img_loss * (image_label == 1).unsqueeze(2).float()
                    ) / max(
                        torch.sum((image_label == 1).unsqueeze(2).expand_as(img_loss)),
                        1,
                    )

                elif self.visual_target == 0:
                    img_loss = self.vis_criterion(
                        F.log_softmax(prediction_scores_v, dim=2), image_target
                    )

                    masked_img_loss = torch.sum(
                        img_loss * (image_label == 1).unsqueeze(2).float()
                    ) / max(torch.sum((image_label == 1)), 0)
                elif self.visual_target == 2:
                    # generate negative sampled index.
                    num_across_batch = int(self.num_negative * 0.7)
                    num_inside_batch = int(self.num_negative * 0.3)

                    batch_size, num_regions, _ = prediction_scores_v.size()
                    assert batch_size != 0
                    # random negative across batches.
                    row_across_index = input_ids.new(
                        batch_size, num_regions, num_across_batch
                    ).random_(0, batch_size - 1)
                    col_across_index = input_ids.new(
                        batch_size, num_regions, num_across_batch
                    ).random_(0, num_regions)

                    for i in range(batch_size - 1):
                        row_across_index[i][row_across_index[i] == i] = batch_size - 1
                    final_across_index = (
                        row_across_index * num_regions + col_across_index
                    )

                    # random negative inside batches.
                    row_inside_index = input_ids.new(
                        batch_size, num_regions, num_inside_batch
                    ).zero_()
                    col_inside_index = input_ids.new(
                        batch_size, num_regions, num_inside_batch
                    ).random_(0, num_regions - 1)

                    for i in range(batch_size):
                        row_inside_index[i] = i
                    for i in range(num_regions - 1):
                        col_inside_index[:, i, :][col_inside_index[:, i, :] == i] = (
                            num_regions - 1
                        )
                    final_inside_index = (
                        row_inside_index * num_regions + col_inside_index
                    )

                    final_index = torch.cat(
                        (final_across_index, final_inside_index), dim=2
                    )

                    # Let's first sample where we need to compute.
                    predict_v = prediction_scores_v[image_label == 1]
                    neg_index_v = final_index[image_label == 1]

                    flat_image_target = image_target.view(batch_size * num_regions, -1)
                    # we also need to append the target feature at the begining.
                    negative_v = flat_image_target[neg_index_v]
                    positive_v = image_target[image_label == 1]
                    sample_v = torch.cat((positive_v.unsqueeze(1), negative_v), dim=1)

                    # calculate the loss.
                    score = torch.bmm(sample_v, predict_v.unsqueeze(2)).squeeze(2)
                    masked_img_loss = self.vis_criterion(
                        score, input_ids.new(score.size(0)).zero_()
                    )

                output_dict["masked_img_loss"] = masked_img_loss.unsqueeze(0)

            masked_lm_loss = self.loss_fct(
                prediction_scores_t.view(-1, self.config.vocab_size),
                masked_lm_labels.view(-1),
            )
            output_dict["masked_lm_loss"] = masked_lm_loss.unsqueeze(0)
            # next_sentence_loss = self.loss_fct(
            #     seq_relationship_score.view(-1, 2), next_sentence_label.view(-1)
            # )
            # output_dict["next_sentence_loss"] = next_sentence_loss.unsqueeze(0)

        else:
            if self.fusion_method == "sum":
                pooled_output = self.dropout(pooled_output_t + pooled_output_v)
            elif self.fusion_method == "mul":
                pooled_output = self.dropout(pooled_output_t * pooled_output_v)
            else:
                assert False

            if "vqa" in self.training_head_type or self.training_head_type == "vizwiz":
                logits = self.classifier(pooled_output)
                output_dict["scores"] = logits.contiguous().view(
                    -1, self.answer_space_size
                )
            elif self.training_head_type == "nlvr2":
                output_dict["scores"] = self.classifier(
                    pooled_output.view(-1, pooled_output.size(1) * 2)
                )
            elif self.training_head_type == "visual_entailment":
                output_dict["scores"] = self.classifier(pooled_output)
            elif self.training_head_type == "mmimdb":
                output_dict["scores"] = self.classifier(pooled_output)

        return output_dict


@registry.register_model("vilbert")
class ViLBERT(BaseModel):
    def __init__(self, config):
        super().__init__(config)
        self.text_only = config.text_only
        self.training_head_type = config.training_head_type

    def build(self):
        self.bert = BertForMultiModalPreTraining.from_pretrained(
            self.config.bert_model_name,
            config=BertConfig.from_dict(self.config),
            cache_dir=os.path.join(
                str(PYTORCH_PRETRAINED_BERT_CACHE), "distributed_{}".format(-1)
            ),
            training_head_type=self.config.training_head_type,
        )
        # if self.config.special_visual_initialize:
        #     self.bert.bert.embeddings.special_intialize()

        if getattr(self.config, "freeze_base", False):
            for p in self.bert.bert.parameters():
                p.requires_grad = False

    def forward(self, sample_list):
        params = self.get_image_and_text_features(sample_list)
        # pretraining labels
        params["masked_lm_labels"] = getattr(sample_list, "lm_label_ids", None)
        # is_random_next = getattr(sample_list, "is_correct", None)
        # TODO(aps): Fix on dataset side
        # params["is_random_next"] = None

        # Prepare Mask
        if params["image_feat"] is not None and params["image_dim"] is not None:
            image_mask = (
                torch.arange(params["image_feat"].size(-2))
                .expand(*params["image_feat"].size()[:-1])
                .cuda()
            )
            if len(params["image_dim"].size()) < len(image_mask.size()):
                params["image_dim"] = params["image_dim"].unsqueeze(-1)
                assert len(params["image_dim"].size()) == len(image_mask.size())
            image_mask = image_mask < params["image_dim"]
            params["image_attention_mask"] = image_mask.long()
        else:
            params["image_attention_mask"] = None
        params.pop("image_dim")

        output_dict = self.bert(**params)

        if "pretraining" in self.training_head_type:
            loss_key = "{}/{}".format(
                sample_list.dataset_name, sample_list.dataset_type
            )
            output_dict["losses"] = {}
            output_dict["losses"][loss_key + "/masked_lm_loss"] = output_dict.pop(
                "masked_lm_loss"
            )
            output_dict["losses"][loss_key + "/masked_img_loss"] = output_dict.pop(
                "masked_img_loss"
            )
            # if params["is_random_next"] is not None:
            #     output_dict["losses"][loss_key + "/next_sentence_loss"] = output_dict.pop("next_sentence_loss")

        return output_dict

    def get_image_and_text_features(self, sample_list):
        bert_input_ids = sample_list.input_ids
        bert_input_mask = sample_list.input_mask
        bert_input_type_ids = sample_list.segment_ids

        if self.training_head_type == "nlvr2":
            bert_input_ids = torch.cat([bert_input_ids, bert_input_ids])
            bert_input_mask = torch.cat([bert_input_mask, bert_input_mask])
            bert_input_type_ids = torch.cat([bert_input_type_ids, bert_input_type_ids])

            # image input
            img0 = getattr(sample_list, "img0", {})
            image_info = getattr(img0, "image_info_0", {})
            image_dim_variable_0 = getattr(image_info, "max_features", None)
            image_feat_variable_0 = getattr(img0, "image_feature_0", None)

            bbox = np.array(getattr(image_info, "bbox", None), dtype=np.float32)
            image_w = np.array(
                getattr(image_info, "image_width", None), dtype=np.float32
            )
            image_h = np.array(
                getattr(image_info, "image_height", None), dtype=np.float32
            )
            image_location = np.zeros(
                (bbox.shape[0], bbox.shape[1], 5), dtype=np.float32
            )
            image_location[:, :, :4] = bbox
            image_location[:, :, 4] = (
                (image_location[:, :, 3] - image_location[:, :, 1])
                * (image_location[:, :, 2] - image_location[:, :, 0])
                / (image_w * image_h)[:, None]
            )
            image_location[:, :, 0] = image_location[:, :, 0] / image_w[:, None]
            image_location[:, :, 1] = image_location[:, :, 1] / image_h[:, None]
            image_location[:, :, 2] = image_location[:, :, 2] / image_w[:, None]
            image_location[:, :, 3] = image_location[:, :, 3] / image_h[:, None]
            image_loc_variable_0 = torch.tensor(
                image_location, dtype=torch.float
            ).cuda()

            img1 = getattr(sample_list, "img1", {})
            image_info = getattr(img1, "image_info_0", {})
            image_dim_variable_1 = getattr(image_info, "max_features", None)
            image_feat_variable_1 = getattr(img1, "image_feature_0", None)
            bbox = np.array(getattr(image_info, "bbox", None), dtype=np.float32)
            image_w = np.array(
                getattr(image_info, "image_width", None), dtype=np.float32
            )
            image_h = np.array(
                getattr(image_info, "image_height", None), dtype=np.float32
            )
            image_location = np.zeros(
                (bbox.shape[0], bbox.shape[1], 5), dtype=np.float32
            )
            image_location[:, :, :4] = bbox
            image_location[:, :, 4] = (
                (image_location[:, :, 3] - image_location[:, :, 1])
                * (image_location[:, :, 2] - image_location[:, :, 0])
                / (image_w * image_h)[:, None]
            )
            image_location[:, :, 0] = image_location[:, :, 0] / image_w[:, None]
            image_location[:, :, 1] = image_location[:, :, 1] / image_h[:, None]
            image_location[:, :, 2] = image_location[:, :, 2] / image_w[:, None]
            image_location[:, :, 3] = image_location[:, :, 3] / image_h[:, None]
            image_loc_variable_1 = torch.tensor(
                image_location, dtype=torch.float
            ).cuda()

            image_feat_variable = torch.cat(
                [image_feat_variable_0, image_feat_variable_1]
            )
            image_loc_variable = torch.cat([image_loc_variable_0, image_loc_variable_1])
            image_dim_variable = torch.cat([image_dim_variable_0, image_dim_variable_1])
            image_label_variable = None
            image_target_variable = None
        else:
            image_info = getattr(sample_list, "image_info_0", {})
            image_dim_variable = getattr(image_info, "max_features", None)
            image_feat_variable = getattr(sample_list, "image_feature_0", None)
            image_label_variable = getattr(sample_list, "image_labels", None)
            if image_label_variable is not None:
                image_label_variable = torch.tensor(
                    image_label_variable, dtype=torch.long
                ).cuda()

            bbox = np.array(getattr(image_info, "bbox", None), dtype=np.float32)
            image_w = np.array(
                getattr(image_info, "image_width", None), dtype=np.float32
            )
            image_h = np.array(
                getattr(image_info, "image_height", None), dtype=np.float32
            )
            image_location = np.zeros(
                (bbox.shape[0], bbox.shape[1], 5), dtype=np.float32
            )
            image_location[:, :, :4] = bbox
            image_location[:, :, 4] = (
                (image_location[:, :, 3] - image_location[:, :, 1])
                * (image_location[:, :, 2] - image_location[:, :, 0])
                / (image_w * image_h)[:, None]
            )
            image_location[:, :, 0] = image_location[:, :, 0] / image_w[:, None]
            image_location[:, :, 1] = image_location[:, :, 1] / image_h[:, None]
            image_location[:, :, 2] = image_location[:, :, 2] / image_w[:, None]
            image_location[:, :, 3] = image_location[:, :, 3] / image_h[:, None]
            image_loc_variable = torch.tensor(image_location, dtype=torch.float).cuda()

            cls_prob = getattr(image_info, "cls_prob", None)
            image_target = np.array(cls_prob, dtype=np.float32)
            image_target_variable = torch.tensor(image_target, dtype=torch.float).cuda()

        return {
            "input_ids": bert_input_ids,
            "attention_mask": bert_input_mask,
            "token_type_ids": bert_input_type_ids,
            "image_dim": image_dim_variable,
            "image_feat": image_feat_variable,
            "image_loc": image_loc_variable,
            "image_target": image_target_variable,
            "image_label": image_label_variable,
        }

    def get_metrics(self, reset: bool = False):
        if (
            self.training_head_type == "nlvr2"
            or self.training_head_type == "multichoice"
            or "vqa" in self.training_head_type
            or self.training_head_type == "flickr"
        ):
            return {"accuracy": self._accuracy.get_metric(reset)}
        return {"accuracy": 0.0}

    def get_optimizer_parameters(self, config):
        # Pretraining has same LR for all of the parts
        if self.training_head_type == "pretraining":
            return self.get_bert_configured_parameters(self)

        # For finetuning setup, we have classifier
        lr = config.optimizer_attributes.params.lr
        vb_config = getattr(config.model_attributes, "vilbert", {})
        finetune_lr_multiplier = getattr(vb_config, "finetune_lr_multiplier", 1)
        # Finetune the bert pretrained part with finetune_lr_multiplier if it is set
        parameters = self.get_bert_configured_parameters(
            self.bert.bert, lr * finetune_lr_multiplier
        )
        # Classifier will be trained on the normal lr
        parameters += self.get_bert_configured_parameters(self.bert.classifier, lr)

        return parameters

    def get_bert_configured_parameters(self, module, lr=None):
        param_optimizer = list(module.named_parameters())

        no_decay = ["bias", "LayerNorm.bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [
                    p for n, p in param_optimizer if not any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.01,
            },
            {
                "params": [
                    p for n, p in param_optimizer if any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.0,
            },
        ]

        if lr is not None:
            for p in optimizer_grouped_parameters:
                p["lr"] = lr

        return optimizer_grouped_parameters

    @staticmethod
    def compute_score_with_logits(logits, labels):
        logits = masked_unk_softmax(logits, 1, 0)
        logits = torch.max(logits, 1)[1].data  # argmax
        one_hots = torch.zeros(*labels.size())
        one_hots = one_hots.cuda() if use_cuda else one_hots
        one_hots.scatter_(1, logits.view(-1, 1), 1)
        scores = one_hots * labels
        return scores
