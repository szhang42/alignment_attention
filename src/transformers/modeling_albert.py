# coding=utf-8
# Copyright 2018 Google AI, Google Brain and the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""PyTorch ALBERT model. """

import logging
import math
import os
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import CrossEntropyLoss, MSELoss

from .configuration_albert import AlbertConfig
from .file_utils import add_start_docstrings, add_start_docstrings_to_callable
from .modeling_bert import ACT2FN, BertEmbeddings, BertSelfAttention, prune_linear_layer
from .modeling_utils import PreTrainedModel
from torch.autograd import Variable
from torch.autograd import Function
import ot
# import sinkhorn_pointcloud as spc
# from geomloss import SamplesLoss


logger = logging.getLogger(__name__)


ALBERT_PRETRAINED_MODEL_ARCHIVE_MAP = {
    "albert-base-v1": "https://cdn.huggingface.co/albert-base-v1-pytorch_model.bin",
    "albert-large-v1": "https://cdn.huggingface.co/albert-large-v1-pytorch_model.bin",
    "albert-xlarge-v1": "https://cdn.huggingface.co/albert-xlarge-v1-pytorch_model.bin",
    "albert-xxlarge-v1": "https://cdn.huggingface.co/albert-xxlarge-v1-pytorch_model.bin",
    "albert-base-v2": "https://cdn.huggingface.co/albert-base-v2-pytorch_model.bin",
    "albert-large-v2": "https://cdn.huggingface.co/albert-large-v2-pytorch_model.bin",
    "albert-xlarge-v2": "https://cdn.huggingface.co/albert-xlarge-v2-pytorch_model.bin",
    "albert-xxlarge-v2": "https://cdn.huggingface.co/albert-xxlarge-v2-pytorch_model.bin",
}


def load_tf_weights_in_albert(model, config, tf_checkpoint_path):
    """ Load tf checkpoints in a pytorch model."""
    try:
        import re
        import numpy as np
        import tensorflow as tf
    except ImportError:
        logger.error(
            "Loading a TensorFlow model in PyTorch, requires TensorFlow to be installed. Please see "
            "https://www.tensorflow.org/install/ for installation instructions."
        )
        raise
    tf_path = os.path.abspath(tf_checkpoint_path)
    logger.info("Converting TensorFlow checkpoint from {}".format(tf_path))
    # Load weights from TF model
    init_vars = tf.train.list_variables(tf_path)
    names = []
    arrays = []
    for name, shape in init_vars:
        logger.info("Loading TF weight {} with shape {}".format(name, shape))
        array = tf.train.load_variable(tf_path, name)
        names.append(name)
        arrays.append(array)

    for name, array in zip(names, arrays):
        print(name)

    for name, array in zip(names, arrays):
        original_name = name

        # If saved from the TF HUB module
        name = name.replace("module/", "")

        # Renaming and simplifying
        name = name.replace("ffn_1", "ffn")
        name = name.replace("bert/", "albert/")
        name = name.replace("attention_1", "attention")
        name = name.replace("transform/", "")
        name = name.replace("LayerNorm_1", "full_layer_layer_norm")
        name = name.replace("LayerNorm", "attention/LayerNorm")
        name = name.replace("transformer/", "")

        # The feed forward layer had an 'intermediate' step which has been abstracted away
        name = name.replace("intermediate/dense/", "")
        name = name.replace("ffn/intermediate/output/dense/", "ffn_output/")

        # ALBERT attention was split between self and output which have been abstracted away
        name = name.replace("/output/", "/")
        name = name.replace("/self/", "/")

        # The pooler is a linear layer
        name = name.replace("pooler/dense", "pooler")

        # The classifier was simplified to predictions from cls/predictions
        name = name.replace("cls/predictions", "predictions")
        name = name.replace("predictions/attention", "predictions")

        # Naming was changed to be more explicit
        name = name.replace("embeddings/attention", "embeddings")
        name = name.replace("inner_group_", "albert_layers/")
        name = name.replace("group_", "albert_layer_groups/")

        # Classifier
        if len(name.split("/")) == 1 and ("output_bias" in name or "output_weights" in name):
            name = "classifier/" + name

        # No ALBERT model currently handles the next sentence prediction task
        if "seq_relationship" in name:
            continue

        name = name.split("/")

        # Ignore the gradients applied by the LAMB/ADAM optimizers.
        if (
            "adam_m" in name
            or "adam_v" in name
            or "AdamWeightDecayOptimizer" in name
            or "AdamWeightDecayOptimizer_1" in name
            or "global_step" in name
        ):
            logger.info("Skipping {}".format("/".join(name)))
            continue

        pointer = model
        for m_name in name:
            if re.fullmatch(r"[A-Za-z]+_\d+", m_name):
                scope_names = re.split(r"_(\d+)", m_name)
            else:
                scope_names = [m_name]

            if scope_names[0] == "kernel" or scope_names[0] == "gamma":
                pointer = getattr(pointer, "weight")
            elif scope_names[0] == "output_bias" or scope_names[0] == "beta":
                pointer = getattr(pointer, "bias")
            elif scope_names[0] == "output_weights":
                pointer = getattr(pointer, "weight")
            elif scope_names[0] == "squad":
                pointer = getattr(pointer, "classifier")
            else:
                try:
                    pointer = getattr(pointer, scope_names[0])
                except AttributeError:
                    logger.info("Skipping {}".format("/".join(name)))
                    continue
            if len(scope_names) >= 2:
                num = int(scope_names[1])
                pointer = pointer[num]

        if m_name[-11:] == "_embeddings":
            pointer = getattr(pointer, "weight")
        elif m_name == "kernel":
            array = np.transpose(array)
        try:
            assert pointer.shape == array.shape
        except AssertionError as e:
            e.args += (pointer.shape, array.shape)
            raise
        print("Initialize PyTorch weight {} from {}".format(name, original_name))
        pointer.data = torch.from_numpy(array)

    return model


class GradReverse(Function):
	@staticmethod
	def forward(ctx, x, beta):
		ctx.beta = beta
		return x.view_as(x)

	@staticmethod
	def backward(ctx, grad_output):
		grad_input = grad_output.neg() * ctx.beta
		return grad_input, None


# Adapted from https://github.com/gpeyre/SinkhornAutoDiff
# Adapted from https://github.com/dfdazac/wassdistance/blob/master/layers.py
class SinkhornDistance(nn.Module):
    r"""
    Given two empirical measures each with :math:`P_1` locations
    :math:`x\in\mathbb{R}^{D_1}` and :math:`P_2` locations :math:`y\in\mathbb{R}^{D_2}`,
    outputs an approximation of the regularized OT cost for point clouds.

    Args:
        eps (float): regularization coefficient
        max_iter (int): maximum number of Sinkhorn iterations
        reduction (string, optional): Specifies the reduction to apply to the output:
            'none' | 'mean' | 'sum'. 'none': no reduction will be applied,
            'mean': the sum of the output will be divided by the number of
            elements in the output, 'sum': the output will be summed. Default: 'none'

    Shape:
        - Input: :math:`(N, P_1, D_1)`, :math:`(N, P_2, D_2)`
        - Output: :math:`(N)` or :math:`()`, depending on `reduction`
    """
    def __init__(self, eps, max_iter, reduction='none'):
        super(SinkhornDistance, self).__init__()
        self.eps = eps
        self.max_iter = max_iter
        self.reduction = reduction

    def forward(self, x, y):
        # The Sinkhorn algorithm takes as input three variables :
        C = self._cost_matrix(x, y)  # Wasserstein cost function
        x_points = x.shape[-2]
        y_points = y.shape[-2]
        if x.dim() == 2:
            batch_size = 1
        else:
            batch_size = x.shape[0]

        # both marginals are fixed with equal weights
        mu = torch.empty(batch_size, x_points, dtype=torch.float,
                         requires_grad=False).fill_(1.0 / x_points).squeeze().cuda()
        nu = torch.empty(batch_size, y_points, dtype=torch.float,
                         requires_grad=False).fill_(1.0 / y_points).squeeze().cuda()

        u = torch.zeros_like(mu)
        v = torch.zeros_like(nu)
        # To check if algorithm terminates because of threshold
        # or max iterations reached
        actual_nits = 0
        # Stopping criterion
        thresh = 1e-1

        # Sinkhorn iterations
        for i in range(self.max_iter):
            u1 = u  # useful to check the update
            u = self.eps * (torch.log(mu+1e-8) - torch.logsumexp(self.M(C, u, v), dim=-1)) + u
            v = self.eps * (torch.log(nu+1e-8) - torch.logsumexp(self.M(C, u, v).transpose(-2, -1), dim=-1)) + v
            err = (u - u1).abs().sum(-1).mean()

            actual_nits += 1
            if err.item() < thresh:
                break

        U, V = u, v
        # Transport plan pi = diag(a)*K*diag(b)
        pi = torch.exp(self.M(C, U, V))
        # Sinkhorn distance
        cost = torch.sum(pi * C, dim=(-2, -1))

        if self.reduction == 'mean':
            cost = cost.mean()
        elif self.reduction == 'sum':
            cost = cost.sum()

        # import pdb
        # pdb.set_trace()
        return cost, pi, C

    @staticmethod
    def _cost_matrix(x, y, p=2):
        "Returns the matrix of $|x_i-y_j|^p$."

        x_col = x.unsqueeze(-2)
        y_lin = y.unsqueeze(-3)
        print(x_col.shape, y_lin.shape)
        C = torch.sum((torch.abs(x_col - y_lin)) ** p, -1)
        return C

    def M(self, C, u, v):
        "Modified cost for logarithmic updates"
        "$M_{ij} = (-c_{ij} + u_i + v_j) / \epsilon$"
        return (-C + u.unsqueeze(-1) + v.unsqueeze(-2)) / self.eps

    @staticmethod

    def ave(u, u1, tau):
        "Barycenter subroutine, used by kinetic acceleration through extrapolation."
        return tau * u + (1 - tau) * u1


class AlbertEmbeddings(BertEmbeddings):
    """
    Construct the embeddings from word, position and token_type embeddings.
    """

    def __init__(self, config):
        super().__init__(config)

        self.word_embeddings = nn.Embedding(config.vocab_size, config.embedding_size, padding_idx=0)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.embedding_size)
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.embedding_size)
        self.LayerNorm = torch.nn.LayerNorm(config.embedding_size, eps=config.layer_norm_eps)

eps = 1e-20
class AlbertAttention(BertSelfAttention):
    def __init__(self, config):
        super().__init__(config)

        self.output_attentions = config.output_attentions
        self.num_attention_heads = config.num_attention_heads
        self.hidden_size = config.hidden_size
        self.attention_head_size = config.hidden_size // config.num_attention_heads
        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)
        self.dropout_two = nn.Dropout(config.attention_probs_dropout_prob)
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.highway_act = nn.Linear(self.attention_head_size, self.attention_head_size)
        self.highway_act_two = nn.Linear(self.attention_head_size, self.attention_head_size)
        self.softmax_act = nn.LogSoftmax()
        self.linear_act = nn.Linear(self.attention_head_size, self.attention_head_size)
        self.criterion = nn.BCEWithLogitsLoss()
        # self.grl1 = GradientReverseLayer()
        # self.grl2 = GradientReverseLayer()


        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.pruned_heads = set()

        self.att_type = config.att_type
        self.adver_type = config.adver_type
        self.rho = config.rho
        self.k_weibull = torch.tensor(config.k_weibull).type(torch.float32)
        self.sigma_normal_posterior = torch.tensor(config.sigma_normal_posterior).type(torch.float32)
        self.att_prior_type = config.att_prior_type
        self.KL_backward = 0
        self.prior_att_weights = None
        self.att_weights = 0

        self.att_contextual_se = config.att_contextual_se
        self.att_se_hid_size = config.att_se_hid_size
        self.att_se_nonlinear = config.att_se_nonlinear
        # self.k_parameterization = config.k_parameterization

        if self.att_prior_type == 'contextual':
            # self.attention_head_size: 64; self.att_se_hid_size: 10
            self.se_linear1 = nn.Linear(self.attention_head_size, self.att_se_hid_size)
            self.se_linear2 = nn.Linear(self.att_se_hid_size, 1)

            self.se_linear3 = nn.Linear(self.attention_head_size, self.att_se_hid_size)
            self.se_linear4 = nn.Linear(self.att_se_hid_size, self.att_se_hid_size)

            self.se_linear5 = nn.Linear(self.attention_head_size, self.att_se_hid_size)
            self.se_linear6 = nn.Linear(self.att_se_hid_size, self.att_se_hid_size)

            self.se_linear7 = nn.Linear(self.attention_head_size, self.att_se_hid_size)
            self.se_linear8 = nn.Linear(self.att_se_hid_size, self.att_se_hid_size)

            self.se_linear9 = nn.Linear(self.attention_head_size, self.att_se_hid_size)
            self.se_linear10 = nn.Linear(self.att_se_hid_size, self.att_se_hid_size)

            #talking head
            self.se_linear11 = nn.Linear(12, 12)
            self.se_linear12 = nn.Linear(12, 12)
            self.se_linear13 = nn.Linear(12, 12)

            self.se_linear1.weight.data.normal_(0, np.sqrt(1 / self.attention_head_size))  # TODO: tune
            self.se_linear2.weight.data.normal_(0, np.sqrt(1.0 / self.att_se_hid_size))


            self.alpha_gamma = nn.Parameter(torch.Tensor(1))
            self.alpha_gamma.data.fill_(config.alpha_gamma)

            if config.att_se_nonlinear == 'lrelu':
                self.se_nonlinear = nn.LeakyReLU(0.1) #TODO: tune.
                self.se_nonlinear2 = nn.LeakyReLU(0.1)
                self.se_nonlinear3 = nn.LeakyReLU(0.1)  # TODO: tune.
                self.se_nonlinear4 = nn.LeakyReLU(0.1)
            elif config.att_se_nonlinear == 'relu':
                self.se_nonlinear = nn.ReLU()
                self.se_nonlinear2 = nn.ReLU()
                self.se_nonlinear3 = nn.ReLU()
                self.se_nonlinear4 = nn.ReLU()
            elif config.att_se_nonlinear == 'tanh':
                self.se_nonlinear = nn.Tanh()
                self.se_nonlinear2 = nn.Tanh()
                self.se_nonlinear3 = nn.Tanh()
                self.se_nonlinear4 = nn.Tanh()


        if self.att_type == 'soft_weibull':
            if config.att_prior_type == 'parameter':
                self.alpha_gamma = nn.Parameter(torch.Tensor(1))
                self.alpha_gamma.data.fill_(config.alpha_gamma)
                self.beta_gamma = torch.tensor(config.beta_gamma).type(torch.float32)
            else:
                self.alpha_gamma = torch.tensor(config.alpha_gamma).type(torch.float32)
                self.beta_gamma = torch.tensor(config.beta_gamma).type(torch.float32)
        elif self.att_type == 'soft_lognormal':
            if config.att_prior_type == 'parameter':
                self.sigma_normal_prior = nn.Parameter(torch.Tensor(1))
                self.sigma_normal_prior.data.fill_(config.sigma_normal_prior)
            else:
                self.sigma_normal_prior = torch.tensor(config.sigma_normal_prior).type(torch.float32)
            self.mean_normal_prior = torch.tensor(0.0).type(torch.float32)

    def discriminator_for(self, x):
        """
        Args:
            x: (batch_size * seq_len)
        """
        pred= x
        highway = self.highway_act(x)  # batch_size * num_filters_sum
        # highway = self.highway(pred)
        pred = torch.sigmoid(highway) *  F.relu(highway) + (1. - torch.sigmoid(highway)) * pred


        # pred = self.linear_act(self.dropout(pred))
        pred =self.se_linear2(self.se_nonlinear(self.se_linear1(self.dropout(pred))))

        return pred


    def discriminator_for_two(self, x):
        """
        Args:
            x: (batch_size * seq_len)
        """
        pred= x
        highway = self.highway_act_two(x)  # batch_size * num_filters_sum
        # highway = self.highway(pred)
        pred = torch.sigmoid(highway) *  F.relu(highway) + (1. - torch.sigmoid(highway)) * pred


        # pred = self.linear_act(self.dropout(pred))
        pred =self.se_linear8(self.se_nonlinear3(self.se_linear7(self.dropout_two(pred))))

        return pred



    def critic_for(self, x):
        """
        Args:
            x: (batch_size * seq_len)
        """
        pred= x
        highway = self.highway_act(x)  # batch_size * num_filters_sum
        # highway = self.highway(pred)
        pred = torch.sigmoid(highway) *  F.relu(highway) + (1. - torch.sigmoid(highway)) * pred


        # pred = self.linear_act(self.dropout(pred))

        pred =self.se_linear4(self.se_nonlinear(self.se_linear3(self.dropout(pred))))
        eps= 1e-6


        return pred / (pred.norm(p=2, dim=-1, keepdim=True) +eps)
        # return torch.softmax(pred, dim=-1)

    def critic_for_two(self, x):
        """
        Args:
            x: (batch_size * seq_len)
        """
        pred= x
        highway = self.highway_act_two(x)  # batch_size * num_filters_sum
        # highway = self.highway(pred)
        pred = torch.sigmoid(highway) *  F.relu(highway) + (1. - torch.sigmoid(highway)) * pred
        pred =self.se_linear8(self.se_nonlinear3(self.se_linear7(self.dropout_two(pred))))
        eps= 1e-6

        return pred / (pred.norm(p=2, dim=-1, keepdim=True) +eps)


    def navigator_for(self, x):
        eps = 1e-6
        x = self.se_linear6(self.se_nonlinear2(self.se_linear5(x)))
        # import pdb
        # pdb.set_trace()
        logits= x/(x.norm(p=2, dim=-1, keepdim=True) +eps)
        # logits = torch.softmax(x, dim=-1)
        return logits


    def navigator_for_two(self, x):
        eps = 1e-6
        x = self.se_linear10(self.se_nonlinear4(self.se_linear9(x)))
        logits= x/(x.norm(p=2, dim=-1, keepdim=True) +eps)
        # logits = torch.softmax(x, dim=-1)
        return logits


    def fast_cdist(self, x1, x2):
        adjustment = x1.mean(-2, keepdim=True)
        x1 = x1 - adjustment
        x2 = x2 - adjustment  # x1 and x2 should be identical in all dims except -2 at this point

        # Compute squared distance matrix using quadratic expansion
        x1_norm = x1.pow(2).sum(dim=-1, keepdim=True)
        x1_pad = torch.ones_like(x1_norm)
        x2_norm = x2.pow(2).sum(dim=-1, keepdim=True)
        x2_pad = torch.ones_like(x2_norm)
        x1_ = torch.cat([-2. * x1, x1_norm, x1_pad], dim=-1)
        x2_ = torch.cat([x2, x2_pad, x2_norm], dim=-1)
        res = x1_.matmul(x2_.transpose(-2, -1))
        # res = x1 @ x2.transpose(-2, -1)
        # Zero out negative values
        res.clamp_min_(1e-30).sqrt_()
        return res


    def prune_heads(self, heads):
        if len(heads) == 0:
            return
        mask = torch.ones(self.num_attention_heads, self.attention_head_size)
        heads = set(heads) - self.pruned_heads  # Convert to set and emove already pruned heads
        for head in heads:
            # Compute how many pruned heads are before the head and move the index accordingly
            head = head - sum(1 if h < head else 0 for h in self.pruned_heads)
            mask[head] = 0
        mask = mask.view(-1).contiguous().eq(1)
        index = torch.arange(len(mask))[mask].long()

        # Prune linear layers
        self.query = prune_linear_layer(self.query, index)
        self.key = prune_linear_layer(self.key, index)
        self.value = prune_linear_layer(self.value, index)
        self.dense = prune_linear_layer(self.dense, index, dim=1)

        # Update hyper params and store pruned heads
        self.num_attention_heads = self.num_attention_heads - len(heads)
        self.all_head_size = self.attention_head_size * self.num_attention_heads
        self.pruned_heads = self.pruned_heads.union(heads)

    def forward(self, input_ids, attention_mask=None, head_mask=None):
        mixed_query_layer = self.query(input_ids)
        mixed_key_layer = self.key(input_ids)
        mixed_value_layer = self.value(input_ids)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        eps = 1e-20
        attention_probs = nn.Softmax(dim=-1)(attention_scores)
        logprobs = torch.log(attention_probs + eps)


        #Version 2
        self.KL_backward =torch.ones_like(key_layer).cuda().mean()

        # talking head
        if self.adver_type == 'talking_head':
            attention_scores_logits = attention_scores.permute(0, 2, 3, 1)
            attention_scores_logits = self.se_linear11(attention_scores_logits)

            attention_probs_logits = nn.Softmax(dim=-1)(attention_scores_logits.permute(0, 3, 1, 2))

            attention_probs_value = attention_probs_logits.permute(0, 2, 3, 1)
            attention_probs_value = self.se_linear12(attention_probs_value)
            attention_probs_value = attention_probs_value.permute(0, 3, 1, 2)
            attention_probs = attention_probs_value


        if self.training:
            if self.adver_type == 'mmd':
                mmdloss = MMD_loss().cuda()
                self.KL_backward = mmdloss(query_layer, key_layer.detach()).mean()


            if self.adver_type =='gan':
                key_layer_reverse = GradReverse.apply(key_layer, 1)
                query_layer_reverse = GradReverse.apply(query_layer, 1)

                real_out = self.discriminator_for(key_layer_reverse)
                real_label = Variable(torch.ones_like(real_out)).cuda().detach()
                # real_label_reverse = GradReverse.apply(real_label, 1)

                d_loss_real = self.criterion(real_out, real_label)
                fake_query = query_layer_reverse.detach()
                fake_out = self.discriminator_for(fake_query)

                fake_label = Variable(torch.zeros_like(fake_out)).cuda().detach()
                # fake_label_reverse = GradReverse.apply(fake_label, 1)
                d_loss_fake = self.criterion(fake_out, fake_label)
                d_loss = d_loss_real + d_loss_fake


                #================================================

                # real_out_tran = self.discriminator_for_two(key_layer_reverse)
                # real_out_tran = real_out_tran.permute(0, 2, 1, 3)
                # real_label_tran = Variable(torch.ones_like(real_out_tran)).cuda().detach()
                # # real_label_reverse = GradReverse.apply(real_label, 1)
                #
                # d_loss_real_tran = self.criterion(real_out_tran, real_label_tran)
                # fake_query_tran = query_layer_reverse.detach()
                # fake_query_tran= fake_query_tran.permute(0, 2, 1, 3)
                # fake_out_tran = self.discriminator_for_two(fake_query_tran)
                #
                # fake_label_tran = Variable(torch.zeros_like(fake_out_tran)).cuda().detach()
                # # fake_label_reverse = GradReverse.apply(fake_label, 1)
                #
                # d_loss_fake_tran = self.criterion(fake_out_tran, fake_label_tran)
                # d_loss_tran = d_loss_real_tran + d_loss_fake_tran

                # final_loss = d_loss.mean() + d_loss_tran.mean()


                self.KL_backward = d_loss.mean()




            if self.adver_type =='act':

                rho = self.rho
                key_layer_reverse = GradReverse.apply(key_layer, 1)
                query_layer_reverse = GradReverse.apply(query_layer, 1)
                real_out = self.critic_for(key_layer_reverse)
                fake_out = self.critic_for(query_layer_reverse)



                #Version 1
                # cost = torch.cdist(real_out, fake_out, p=2)

                #Version 2

                real_out_tran= real_out.permute(0, 2, 1, 3) #  transpose(1, 2).contiguous()
                fake_out_tran= fake_out.permute(0, 2, 1, 3) #  transpose(1, 2).contiguous()

                cost = self.fast_cdist(real_out_tran, fake_out_tran)



                # Version 1
                n_x = self.navigator_for(key_layer_reverse)
                n_y= self.navigator_for(query_layer_reverse)
                # d = torch.matmul(n_x, n_y.transpose(-1, -2))

                # Version 2
                n_x_tran = n_x.transpose(1, 2)
                n_y_tran= n_y.transpose(1, 2)
                d = torch.matmul(n_x_tran, n_y_tran.transpose(-1, -2))


                m_backward = torch.nn.functional.softmax(d, dim=-2)  # backward transport map key
                m_forward = torch.nn.functional.softmax(d, dim=-1)  # forward transport map query

                errD = - ((1 - rho) * (cost * m_backward).sum(-2).mean() + rho * (cost * m_forward).sum(-1).mean())

                self.KL_backward = errD.mean()



            if self.adver_type =='act_test':

                rho = self.rho
                key_layer_reverse = GradReverse.apply(key_layer, 1)
                query_layer_reverse = GradReverse.apply(query_layer, 1)
                real_out_old = self.critic_for(key_layer_reverse)
                fake_out_old = self.critic_for(query_layer_reverse)

                #Version 1
                # cost = torch.cdist(real_out, fake_out, p=2)

                #Version 2

                real_out = GradReverse.apply(real_out_old, 1)
                fake_out = GradReverse.apply(fake_out_old, 1)

                real_out_tran= real_out.permute(0, 2, 1, 3) #  transpose(1, 2).contiguous()
                fake_out_tran= fake_out.permute(0, 2, 1, 3) #  transpose(1, 2).contiguous()

                cost = self.fast_cdist(real_out_tran, fake_out_tran)

                # Version 1
                n_x = self.navigator_for(key_layer)
                n_y= self.navigator_for(query_layer)
                # d = torch.matmul(n_x, n_y.transpose(-1, -2))

                # Version 2
                n_x_tran = n_x.transpose(1, 2)
                n_y_tran= n_y.transpose(1, 2)
                d = torch.matmul(n_x_tran, n_y_tran.transpose(-1, -2))

                m_backward = torch.nn.functional.softmax(d, dim=-2)  # backward transport map key
                m_forward = torch.nn.functional.softmax(d, dim=-1)  # forward transport map query
                errD = - ((1 - rho) * (cost * m_backward).sum(-2).mean() + rho * (cost * m_forward).sum(-1).mean())

                self.KL_backward = errD.mean()

            if self.adver_type == 'ot':
                n = 200
                eps = 0.01
                max_iter = 100

                batch_size = key_layer.shape[0]
                num_dim = key_layer.shape[-1]
                num_head = key_layer.shape[1]
                num_key= key_layer.shape[2]

                query_layer = query_layer.reshape([-1, num_key, num_dim])
                key_layer = key_layer.reshape([-1, num_key, num_dim])


                # sinkloss= SinkhornDistance(eps, max_iter, reduction='mean').cuda()
                # self.KL_backward= SinkhornDistance(eps, max_iter, reduction='mean')(query_layer, key_layer)[0]


                #geomloss
                loss = SamplesLoss(loss="sinkhorn", p=2, blur=.05)

                L = loss(query_layer, key_layer)  # By default, use constant weights = 1/number of samples
                self.KL_backward = L.mean()
                # self.KL_backward = self.fast_cdist(key_layer, query_layer).mean()
                # Version 2
                # l1 = spc.sinkhorn_loss(x, y, epsilon, n, niter)
                # l2 = spc.sinkhorn_normalized(x, y, epsilon, n, niter)
                #
                # self.KL_backward = l1.data[0]
                # self.KL_backward = l2.data[0]




            if self.adver_type == 'combine':

                rho = self.rho
                real_out = key_layer
                fake_out = query_layer
                # Version 1
                # cost = torch.cdist(real_out, fake_out, p=2)
                cost = self.fast_cdist(real_out, fake_out)

                # Version 2

                # real_out_head = self.critic_for_two(key_layer_reverse)
                # fake_out_head = self.critic_for_two(query_layer_reverse)

                real_out_head = key_layer
                fake_out_head = query_layer

                real_out_tran = real_out_head.permute(0, 2, 1, 3)  # transpose(1, 2).contiguous()
                fake_out_tran = fake_out_head.permute(0, 2, 1, 3)  # transpose(1, 2).contiguous()

                cost_head = self.fast_cdist(real_out_tran, fake_out_tran)

                # Version 1
                # n_x = self.navigator_for(key_layer_reverse)
                # n_y = self.navigator_for(query_layer_reverse)

                n_x = self.navigator_for(key_layer)
                n_y = self.navigator_for(query_layer)
                d = torch.matmul(n_x, n_y.transpose(-1, -2))

                # Version 2
                # n_x_tran = self.navigator_for_two(key_layer_reverse)
                # n_y_tran = self.navigator_for_two(query_layer_reverse)

                n_x_tran = self.navigator_for_two(key_layer)
                n_y_tran = self.navigator_for_two(query_layer)

                n_x_tran = n_x_tran.transpose(1, 2)
                n_y_tran = n_y_tran.transpose(1, 2)
                d_head = torch.matmul(n_x_tran, n_y_tran.transpose(-1, -2))

                m_backward = torch.nn.functional.softmax(d, dim=-2)  # backward transport map key
                m_forward = torch.nn.functional.softmax(d, dim=-1)  # forward transport map query

                m_backward_head = torch.nn.functional.softmax(d_head, dim=-2)  # backward transport map key
                m_forward_head = torch.nn.functional.softmax(d_head, dim=-1)  # forward transport map query

                err = - ((1 - rho) * (cost * m_backward).sum(-2).mean() + rho * (cost * m_forward).sum(-1).mean())
                errHead = - ((1 - rho) * (cost_head * m_backward_head).sum(-2).mean() + rho * (cost_head * m_forward_head).sum(-1).mean())
                #Version 1
                errD = err + errHead
                # errD= torch.sigmoid(self.alpha_gamma) * err + (1-torch.sigmoid(self.alpha_gamma) * errHead)
                self.KL_backward = errD.mean()


        if self.att_prior_type == 'contextual':
            if self.att_type == 'soft_weibull':
                if self.att_se_nonlinear == 'none':
                    dot_gamma = self.se_linear1(key_layer)
                else:
                    dot_gamma = self.se_linear2(self.se_nonlinear(self.se_linear1(key_layer)))
                dot_gamma = dot_gamma.transpose(2, 3)
                if attention_mask is not None:
                    dot_gamma = dot_gamma + attention_mask
                self.prior_att_weights = F.softmax(dot_gamma, dim=-1)
                self.alpha_gamma = self.prior_att_weights * self.beta_gamma
            elif self.att_type == 'soft_lognormal':
                if self.att_se_nonlinear == 'none':
                    dot_mu = self.se_linear1(key_layer)
                else:
                    dot_mu = self.se_linear2(self.se_nonlinear(self.se_linear1(key_layer)))
                dot_mu = dot_mu.transpose(2, 3)
                if attention_mask is not None:
                    dot_mu = dot_mu + attention_mask
                self.prior_att_weights = F.softmax(dot_mu, dim=-1)
                self.mean_normal_prior = torch.log(self.prior_att_weights + eps) #- self.sigma_normal_prior ** 2 / 2

        if self.att_type == 'soft_weibull':
            if self.training:
                if 0:
                    self.alpha_gamma = attention_probs
                    if self.k_parameterization == 'blue':
                        k_weibull = 1.0 #todo
                    elif self.k_parameterization == 'orange':
                        k_weibull = torch.exp(logprobs) + torch.exp(-logprobs)
                    elif self.k_parameterization == 'red':
                        k_weibull = torch.exp(logprobs)
                    lambda_weibull = torch.exp(logprobs - torch.lgamma(1 + 1.0 /k_weibull)) / self.beta_gamma
                    u_weibull = torch.rand_like(logprobs)
                    sample_weibull = lambda_weibull * torch.exp(1.0 / self.k_weibull * torch.log(
                            - torch.log(1.0 - u_weibull + eps) + eps))
                    out_weight = sample_weibull / sample_weibull.sum(-1, keepdim=True)
                    if np.random.uniform() > 0.99:
                        print('k_weibull', k_weibull.mean(), k_weibull.std())
                    KL = -(self.alpha_gamma * torch.log(lambda_weibull + eps) - np.euler_gamma * self.alpha_gamma / k_weibull \
                           -torch.log(k_weibull + eps) - self.beta_gamma * lambda_weibull * torch.exp(
                                                         torch.lgamma(1 + 1.0 / k_weibull)) + \
                           self.alpha_gamma * torch.log(self.beta_gamma + eps) - torch.lgamma(self.alpha_gamma + eps))
                    self.KL_backward = KL.mean()

                else:
                    u_weibull = torch.rand_like(logprobs)
                    out_weight = F.softmax(logprobs - torch.lgamma(1 + 1.0 / self.k_weibull) + 1.0 / self.k_weibull * torch.log(- torch.log(
                        1.0 - u_weibull + eps) + eps), dim=-1)
                    KL = -(self.alpha_gamma * (logprobs - torch.lgamma(1 + 1.0 / self.k_weibull)) - np.euler_gamma * self.alpha_gamma / self.k_weibull \
                         - self.beta_gamma * torch.exp(logprobs - torch.lgamma(1 + 1.0 / self.k_weibull) +
                        torch.lgamma(1 + 1.0 / self.k_weibull)) + \
                         self.alpha_gamma * torch.log(self.beta_gamma + eps) - torch.lgamma(self.alpha_gamma + eps))
                    self.KL_backward = KL.mean()
            else:
                out_weight = attention_probs

        elif self.att_type == 'soft_lognormal':
            if self.training:
                mean_normal_posterior = logprobs - self.sigma_normal_posterior ** 2 / 2
                out_weight = F.softmax(mean_normal_posterior + self.sigma_normal_posterior * torch.randn_like(logprobs), dim=-1)
                KL = torch.log(self.sigma_normal_prior / self.sigma_normal_posterior + eps) + (
                        self.sigma_normal_posterior ** 2 + (mean_normal_posterior - self.mean_normal_prior) ** 2) / (2 * self.sigma_normal_prior ** 2) - 0.5
                self.KL_backward = KL.mean()
            else:
                out_weight = attention_probs
        else:
            out_weight = attention_probs

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
            out_weight = self.dropout(out_weight)

        # Mask heads if we want to
        if head_mask is not None:
            out_weight = out_weight * head_mask

        context_layer = torch.matmul(out_weight, value_layer)

        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()

        # Should find a better way to do this
        w = (
            self.dense.weight.t()
            .view(self.num_attention_heads, self.attention_head_size, self.hidden_size)
            .to(context_layer.dtype)
        )
        b = self.dense.bias.to(context_layer.dtype)

        projected_context_layer = torch.einsum("bfnd,ndh->bfh", context_layer, w) + b
        projected_context_layer_dropout = self.dropout(projected_context_layer)
        layernormed_context_layer = self.LayerNorm(input_ids + projected_context_layer_dropout)
        return (layernormed_context_layer, out_weight) if self.output_attentions else (layernormed_context_layer,)


class AlbertLayer(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.config = config
        self.full_layer_layer_norm = nn.LayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.attention = AlbertAttention(config)
        self.ffn = nn.Linear(config.hidden_size, config.intermediate_size)
        self.ffn_output = nn.Linear(config.intermediate_size, config.hidden_size)
        self.activation = ACT2FN[config.hidden_act]
        self.KL = 0.0

    def forward(self, hidden_states, attention_mask=None, head_mask=None):
        attention_output = self.attention(hidden_states, attention_mask, head_mask)
        self.KL = self.attention.KL_backward
        ffn_output = self.ffn(attention_output[0])
        ffn_output = self.activation(ffn_output)
        ffn_output = self.ffn_output(ffn_output)
        hidden_states = self.full_layer_layer_norm(ffn_output + attention_output[0])

        return (hidden_states,) + attention_output[1:]  # add attentions if we output them


class AlbertLayerGroup(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.output_attentions = config.output_attentions
        self.output_hidden_states = config.output_hidden_states
        self.albert_layers = nn.ModuleList([AlbertLayer(config) for _ in range(config.inner_group_num)])
        self.KL_inner_list = []

    def forward(self, hidden_states, attention_mask=None, head_mask=None):
        layer_hidden_states = ()
        layer_attentions = ()
        self.KL_inner_list = []
        for layer_index, albert_layer in enumerate(self.albert_layers):
            layer_output = albert_layer(hidden_states, attention_mask, head_mask[layer_index])
            self.KL_inner_list.append(albert_layer.KL)
            hidden_states = layer_output[0]

            if self.output_attentions:
                layer_attentions = layer_attentions + (layer_output[1],)

            if self.output_hidden_states:
                layer_hidden_states = layer_hidden_states + (hidden_states,)

        outputs = (hidden_states,)
        if self.output_hidden_states:
            outputs = outputs + (layer_hidden_states,)
        if self.output_attentions:
            outputs = outputs + (layer_attentions,)
        return outputs  # last-layer hidden state, (layer hidden states), (layer attentions)


class AlbertTransformer(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.config = config
        self.output_attentions = config.output_attentions
        self.output_hidden_states = config.output_hidden_states
        self.embedding_hidden_mapping_in = nn.Linear(config.embedding_size, config.hidden_size)
        self.albert_layer_groups = nn.ModuleList([AlbertLayerGroup(config) for _ in range(config.num_hidden_groups)])
        self.KL_list = []

    def forward(self, hidden_states, attention_mask=None, head_mask=None):
        hidden_states = self.embedding_hidden_mapping_in(hidden_states)

        all_attentions = ()

        if self.output_hidden_states:
            all_hidden_states = (hidden_states,)
        self.KL_list = []

        for i in range(self.config.num_hidden_layers):
            # Number of layers in a hidden group
            layers_per_group = int(self.config.num_hidden_layers / self.config.num_hidden_groups)

            # Index of the hidden group
            group_idx = int(i / (self.config.num_hidden_layers / self.config.num_hidden_groups))

            layer_group_output = self.albert_layer_groups[group_idx](
                hidden_states,
                attention_mask,
                head_mask[group_idx * layers_per_group : (group_idx + 1) * layers_per_group],
            )
            hidden_states = layer_group_output[0]
            self.KL_list.append(self.albert_layer_groups[group_idx].KL_inner_list)

            if self.output_attentions:
                all_attentions = all_attentions + layer_group_output[-1]

            if self.output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

        outputs = (hidden_states,)
        if self.output_hidden_states:
            outputs = outputs + (all_hidden_states,)
        if self.output_attentions:
            outputs = outputs + (all_attentions,)
        return outputs  # last-layer hidden state, (all hidden states), (all attentions)


class AlbertPreTrainedModel(PreTrainedModel):
    """ An abstract class to handle weights initialization and
        a simple interface for downloading and loading pretrained models.
    """

    config_class = AlbertConfig
    pretrained_model_archive_map = ALBERT_PRETRAINED_MODEL_ARCHIVE_MAP
    base_model_prefix = "albert"

    def _init_weights(self, module):
        """ Initialize the weights.
        """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if isinstance(module, (nn.Linear)) and module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)


ALBERT_START_DOCSTRING = r"""

    This model is a PyTorch `torch.nn.Module <https://pytorch.org/docs/stable/nn.html#torch.nn.Module>`_ sub-class.
    Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general
    usage and behavior.

    Args:
        config (:class:`~transformers.AlbertConfig`): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the configuration.
            Check out the :meth:`~transformers.PreTrainedModel.from_pretrained` method to load the model weights.
"""

ALBERT_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary.

            Indices can be obtained using :class:`transformers.AlbertTokenizer`.
            See :func:`transformers.PreTrainedTokenizer.encode` and
            :func:`transformers.PreTrainedTokenizer.encode_plus` for details.

            `What are input IDs? <../glossary.html#input-ids>`__
        attention_mask (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`):
            Mask to avoid performing attention on padding token indices.
            Mask values selected in ``[0, 1]``:
            ``1`` for tokens that are NOT MASKED, ``0`` for MASKED tokens.

            `What are attention masks? <../glossary.html#attention-mask>`__
        token_type_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`):
            Segment token indices to indicate first and second portions of the inputs.
            Indices are selected in ``[0, 1]``: ``0`` corresponds to a `sentence A` token, ``1``
            corresponds to a `sentence B` token

            `What are token type IDs? <../glossary.html#token-type-ids>`_
        position_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`):
            Indices of positions of each input sequence tokens in the position embeddings.
            Selected in the range ``[0, config.max_position_embeddings - 1]``.

            `What are position IDs? <../glossary.html#position-ids>`_
        head_mask (:obj:`torch.FloatTensor` of shape :obj:`(num_heads,)` or :obj:`(num_layers, num_heads)`, `optional`, defaults to :obj:`None`):
            Mask to nullify selected heads of the self-attention modules.
            Mask values selected in ``[0, 1]``:
            :obj:`1` indicates the head is **not masked**, :obj:`0` indicates the head is **masked**.
        input_embeds (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`, defaults to :obj:`None`):
            Optionally, instead of passing :obj:`input_ids` you can choose to directly pass an embedded representation.
            This is useful if you want more control over how to convert `input_ids` indices into associated vectors
            than the model's internal embedding lookup matrix.
"""


@add_start_docstrings(
    "The bare ALBERT Model transformer outputting raw hidden-states without any specific head on top.",
    ALBERT_START_DOCSTRING,
)
class AlbertModel(AlbertPreTrainedModel):

    config_class = AlbertConfig
    pretrained_model_archive_map = ALBERT_PRETRAINED_MODEL_ARCHIVE_MAP
    load_tf_weights = load_tf_weights_in_albert
    base_model_prefix = "albert"

    def __init__(self, config):
        super().__init__(config)

        self.config = config
        self.embeddings = AlbertEmbeddings(config)
        self.encoder = AlbertTransformer(config)
        self.pooler = nn.Linear(config.hidden_size, config.hidden_size)
        self.pooler_activation = nn.Tanh()

        self.init_weights()

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def _resize_token_embeddings(self, new_num_tokens):
        old_embeddings = self.embeddings.word_embeddings
        new_embeddings = self._get_resized_embeddings(old_embeddings, new_num_tokens)
        self.embeddings.word_embeddings = new_embeddings
        return self.embeddings.word_embeddings

    def _prune_heads(self, heads_to_prune):
        """ Prunes heads of the model.
            heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
            ALBERT has a different architecture in that its layers are shared across groups, which then has inner groups.
            If an ALBERT model has 12 hidden layers and 2 hidden groups, with two inner groups, there
            is a total of 4 different layers.

            These layers are flattened: the indices [0,1] correspond to the two inner groups of the first hidden layer,
            while [2,3] correspond to the two inner groups of the second hidden layer.

            Any layer with in index other than [0,1,2,3] will result in an error.
            See base class PreTrainedModel for more information about head pruning
        """
        for layer, heads in heads_to_prune.items():
            group_idx = int(layer / self.config.inner_group_num)
            inner_group_idx = int(layer - group_idx * self.config.inner_group_num)
            self.encoder.albert_layer_groups[group_idx].albert_layers[inner_group_idx].attention.prune_heads(heads)

    @add_start_docstrings_to_callable(ALBERT_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
    ):
        r"""
    Return:
        :obj:`tuple(torch.FloatTensor)` comprising various elements depending on the configuration (:class:`~transformers.AlbertConfig`) and inputs:
        last_hidden_state (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`):
            Sequence of hidden-states at the output of the last layer of the model.
        pooler_output (:obj:`torch.FloatTensor`: of shape :obj:`(batch_size, hidden_size)`):
            Last layer hidden-state of the first token of the sequence (classification token)
            further processed by a Linear layer and a Tanh activation function. The Linear
            layer weights are trained from the next sentence prediction (classification)
            objective during pre-training.

            This output is usually *not* a good summary
            of the semantic content of the input, you're often better with averaging or pooling
            the sequence of hidden-states for the whole input sequence.
        hidden_states (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_hidden_states=True``):
            Tuple of :obj:`torch.FloatTensor` (one for the output of the embeddings + one for the output of each layer)
            of shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_attentions=True``):
            Tuple of :obj:`torch.FloatTensor` (one for each layer) of shape
            :obj:`(batch_size, num_heads, sequence_length, sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.

    Example::

        from transformers import AlbertModel, AlbertTokenizer
        import torch

        tokenizer = AlbertTokenizer.from_pretrained('albert-base-v2')
        model = AlbertModel.from_pretrained('albert-base-v2')
        input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute", add_special_tokens=True)).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids)
        last_hidden_states = outputs[0]  # The last hidden-state is the first element of the output tuple

        """

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        if attention_mask is None:
            attention_mask = torch.ones(input_shape, device=device)
        if token_type_ids is None:
            token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=device)

        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)
        extended_attention_mask = extended_attention_mask.to(dtype=torch.float32)  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
        head_mask = self.get_head_mask(head_mask, self.config.num_hidden_layers)

        embedding_output = self.embeddings(
            input_ids, position_ids=position_ids, token_type_ids=token_type_ids, inputs_embeds=inputs_embeds
        )
        encoder_outputs = self.encoder(embedding_output, extended_attention_mask, head_mask=head_mask)

        sequence_output = encoder_outputs[0]

        pooled_output = self.pooler_activation(self.pooler(sequence_output[:, 0]))

        outputs = (sequence_output, pooled_output) + encoder_outputs[
            1:
        ]  # add hidden_states and attentions if they are here
        return outputs


class AlbertMLMHead(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.LayerNorm = nn.LayerNorm(config.embedding_size)
        self.bias = nn.Parameter(torch.zeros(config.vocab_size))
        self.dense = nn.Linear(config.hidden_size, config.embedding_size)
        self.decoder = nn.Linear(config.embedding_size, config.vocab_size)
        self.activation = ACT2FN[config.hidden_act]

        # Need a link between the two variables so that the bias is correctly resized with `resize_token_embeddings`
        self.decoder.bias = self.bias

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.activation(hidden_states)
        hidden_states = self.LayerNorm(hidden_states)
        hidden_states = self.decoder(hidden_states)

        prediction_scores = hidden_states

        return prediction_scores


@add_start_docstrings(
    "Albert Model with a `language modeling` head on top.", ALBERT_START_DOCSTRING,
)
class AlbertForMaskedLM(AlbertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.albert = AlbertModel(config)
        self.predictions = AlbertMLMHead(config)

        self.init_weights()
        self.tie_weights()

    def tie_weights(self):
        self._tie_or_clone_weights(self.predictions.decoder, self.albert.embeddings.word_embeddings)

    def get_output_embeddings(self):
        return self.predictions.decoder

    @add_start_docstrings_to_callable(ALBERT_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        masked_lm_labels=None,
    ):
        r"""
        masked_lm_labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`):
            Labels for computing the masked language modeling loss.
            Indices should be in ``[-100, 0, ..., config.vocab_size]`` (see ``input_ids`` docstring)
            Tokens with indices set to ``-100`` are ignored (masked), the loss is only computed for the tokens with
            labels in ``[0, ..., config.vocab_size]``

    Returns:
        :obj:`tuple(torch.FloatTensor)` comprising various elements depending on the configuration (:class:`~transformers.AlbertConfig`) and inputs:
        loss (`optional`, returned when ``masked_lm_labels`` is provided) ``torch.FloatTensor`` of shape ``(1,)``:
            Masked language modeling loss.
        prediction_scores (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length, config.vocab_size)`)
            Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
        hidden_states (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_hidden_states=True``):
            Tuple of :obj:`torch.FloatTensor` (one for the output of the embeddings + one for the output of each layer)
            of shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_attentions=True``):
            Tuple of :obj:`torch.FloatTensor` (one for each layer) of shape
            :obj:`(batch_size, num_heads, sequence_length, sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.

    Example::

        from transformers import AlbertTokenizer, AlbertForMaskedLM
        import torch

        tokenizer = AlbertTokenizer.from_pretrained('albert-base-v2')
        model = AlbertForMaskedLM.from_pretrained('albert-base-v2')
        input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute", add_special_tokens=True)).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids, masked_lm_labels=input_ids)
        loss, prediction_scores = outputs[:2]

        """
        outputs = self.albert(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
        )
        sequence_outputs = outputs[0]

        prediction_scores = self.predictions(sequence_outputs)

        outputs = (prediction_scores,) + outputs[2:]  # Add hidden states and attention if they are here
        if masked_lm_labels is not None:
            loss_fct = CrossEntropyLoss()
            masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))
            outputs = (masked_lm_loss,) + outputs

        return outputs


@add_start_docstrings(
    """Albert Model transformer with a sequence classification/regression head on top (a linear layer on top of
    the pooled output) e.g. for GLUE tasks. """,
    ALBERT_START_DOCSTRING,
)
class AlbertForSequenceClassification(AlbertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels

        self.albert = AlbertModel(config)
        self.dropout = nn.Dropout(config.classifier_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, self.config.num_labels)

        self.init_weights()

    @add_start_docstrings_to_callable(ALBERT_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`, defaults to :obj:`None`):
            Labels for computing the sequence classification/regression loss.
            Indices should be in ``[0, ..., config.num_labels - 1]``.
            If ``config.num_labels == 1`` a regression loss is computed (Mean-Square loss),
            If ``config.num_labels > 1`` a classification loss is computed (Cross-Entropy).

    Returns:
        :obj:`tuple(torch.FloatTensor)` comprising various elements depending on the configuration (:class:`~transformers.AlbertConfig`) and inputs:
        loss: (`optional`, returned when ``labels`` is provided) ``torch.FloatTensor`` of shape ``(1,)``:
            Classification (or regression if config.num_labels==1) loss.
        logits ``torch.FloatTensor`` of shape ``(batch_size, config.num_labels)``
            Classification (or regression if config.num_labels==1) scores (before SoftMax).
        hidden_states (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_hidden_states=True``):
            Tuple of :obj:`torch.FloatTensor` (one for the output of the embeddings + one for the output of each layer)
            of shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_attentions=True``):
            Tuple of :obj:`torch.FloatTensor` (one for each layer) of shape
            :obj:`(batch_size, num_heads, sequence_length, sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.

        Examples::

            from transformers import AlbertTokenizer, AlbertForSequenceClassification
            import torch

            tokenizer = AlbertTokenizer.from_pretrained('albert-base-v2')
            model = AlbertForSequenceClassification.from_pretrained('albert-base-v2')
            input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute")).unsqueeze(0)  # Batch size 1
            labels = torch.tensor([1]).unsqueeze(0)  # Batch size 1
            outputs = model(input_ids, labels=labels)
            loss, logits = outputs[:2]

        """

        outputs = self.albert(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
        )

        pooled_output = outputs[1]

        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here

        KL = 0
        count = 0
        for inner_list in self.albert.encoder.KL_list:
            for item in inner_list:
                KL = KL + item
                count = count + 1
        KL = KL / count

        if labels is not None:
            outputs = outputs + (KL,)

            # torch.tensor() 后面没加 required gradient true 就会断gradient
            # outputs = outputs + (torch.tensor(KL).type_as(logits).cuda(),)
            if self.num_labels == 1:
                #  We are doing regression
                loss_fct = MSELoss()
                loss = loss_fct(logits.view(-1), labels.view(-1))
            else:
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            outputs = (loss,) + outputs

        return outputs  # (loss), logits, (hidden_states), (attentions)


@add_start_docstrings(
    """Albert Model with a token classification head on top (a linear layer on top of
    the hidden-states output) e.g. for Named-Entity-Recognition (NER) tasks. """,
    ALBERT_START_DOCSTRING,
)
class AlbertForTokenClassification(AlbertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels

        self.albert = AlbertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, self.config.num_labels)

        self.init_weights()

    @add_start_docstrings_to_callable(ALBERT_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`, defaults to :obj:`None`):
            Labels for computing the token classification loss.
            Indices should be in ``[0, ..., config.num_labels - 1]``.

    Returns:
        :obj:`tuple(torch.FloatTensor)` comprising various elements depending on the configuration (:class:`~transformers.AlbertConfig`) and inputs:
        loss (:obj:`torch.FloatTensor` of shape :obj:`(1,)`, `optional`, returned when ``labels`` is provided) :
            Classification loss.
        scores (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length, config.num_labels)`)
            Classification scores (before SoftMax).
        hidden_states (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_hidden_states=True``):
            Tuple of :obj:`torch.FloatTensor` (one for the output of the embeddings + one for the output of each layer)
            of shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_attentions=True``):
            Tuple of :obj:`torch.FloatTensor` (one for each layer) of shape
            :obj:`(batch_size, num_heads, sequence_length, sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.

    Examples::

        from transformers import AlbertTokenizer, AlbertForTokenClassification
        import torch

        tokenizer = AlbertTokenizer.from_pretrained('albert-base-v2')
        model = AlbertForTokenClassification.from_pretrained('albert-base-v2')

        input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute", add_special_tokens=True)).unsqueeze(0)  # Batch size 1
        labels = torch.tensor([1] * input_ids.size(1)).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids, labels=labels)

        loss, scores = outputs[:2]

        """

        outputs = self.albert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
        )

        sequence_output = outputs[0]

        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        outputs = (logits,) + outputs[2:]  # add hidden states and attention if they are here

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            if attention_mask is not None:
                active_loss = attention_mask.view(-1) == 1
                active_logits = logits.view(-1, self.num_labels)[active_loss]
                active_labels = labels.view(-1)[active_loss]
                loss = loss_fct(active_logits, active_labels)
            else:
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            outputs = (loss,) + outputs

        return outputs  # (loss), logits, (hidden_states), (attentions)


@add_start_docstrings(
    """Albert Model with a span classification head on top for extractive question-answering tasks like SQuAD (a linear layers on top of
    the hidden-states output to compute `span start logits` and `span end logits`). """,
    ALBERT_START_DOCSTRING,
)
class AlbertForQuestionAnswering(AlbertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels

        self.albert = AlbertModel(config)
        self.qa_outputs = nn.Linear(config.hidden_size, config.num_labels)
        self.label_noise = config.label_noise

        self.init_weights()

    @add_start_docstrings_to_callable(ALBERT_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        start_positions=None,
        end_positions=None,
    ):
        r"""
        start_positions (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`, defaults to :obj:`None`):
            Labels for position (index) of the start of the labelled span for computing the token classification loss.
            Positions are clamped to the length of the sequence (`sequence_length`).
            Position outside of the sequence are not taken into account for computing the loss.
        end_positions (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`, defaults to :obj:`None`):
            Labels for position (index) of the end of the labelled span for computing the token classification loss.
            Positions are clamped to the length of the sequence (`sequence_length`).
            Position outside of the sequence are not taken into account for computing the loss.

    Returns:
        :obj:`tuple(torch.FloatTensor)` comprising various elements depending on the configuration (:class:`~transformers.AlbertConfig`) and inputs:
        loss: (`optional`, returned when ``labels`` is provided) ``torch.FloatTensor`` of shape ``(1,)``:
            Total span extraction loss is the sum of a Cross-Entropy for the start and end positions.
        start_scores ``torch.FloatTensor`` of shape ``(batch_size, sequence_length,)``
            Span-start scores (before SoftMax).
        end_scores: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length,)``
            Span-end scores (before SoftMax).
        hidden_states (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_hidden_states=True``):
            Tuple of :obj:`torch.FloatTensor` (one for the output of the embeddings + one for the output of each layer)
            of shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_attentions=True``):
            Tuple of :obj:`torch.FloatTensor` (one for each layer) of shape
            :obj:`(batch_size, num_heads, sequence_length, sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.

    Examples::

        # The checkpoint albert-base-v2 is not fine-tuned for question answering. Please see the
        # examples/run_squad.py example to see how to fine-tune a model to a question answering task.

        from transformers import AlbertTokenizer, AlbertForQuestionAnswering
        import torch

        tokenizer = AlbertTokenizer.from_pretrained('albert-base-v2')
        model = AlbertForQuestionAnswering.from_pretrained('albert-base-v2')
        question, text = "Who was Jim Henson?", "Jim Henson was a nice puppet"
        input_dict = tokenizer.encode_plus(question, text, return_tensors='pt')
        start_scores, end_scores = model(**input_dict)

        """

        outputs = self.albert(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
        )

        sequence_output = outputs[0]

        logits = self.qa_outputs(sequence_output)
        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        KL = 0
        count = 0
        for inner_list in self.albert.encoder.KL_list:
            for item in inner_list:
                KL = KL + item
                count = count + 1
        KL = KL / count

        outputs = (start_logits, end_logits, ) + outputs[2:]
        if start_positions is not None and end_positions is not None:
            outputs = outputs + (KL,)

            # torch.tensor() 后面没加 required gradient true 就会断gradient
            # outputs = outputs + (torch.tensor(KL).type_as(logits).cuda(),)
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)

            ber_start = (torch.randn_like(start_positions.type_as(start_logits)) > 1 - self.label_noise).type_as(start_positions)
            start_positions_noise = start_positions - ber_start
            start_positions_noise = torch.max(start_positions_noise, torch.zeros_like(start_positions_noise))

            ber_end = (torch.randn_like(end_positions.type_as(start_logits)) > 1 - self.label_noise).type_as(end_positions)
            end_positions_noise = end_positions - ber_end
            end_positions_noise = torch.max(end_positions_noise, torch.zeros_like(end_positions_noise))

            start_loss = loss_fct(start_logits, start_positions_noise)
            end_loss = loss_fct(end_logits, end_positions_noise)
            total_loss = (start_loss + end_loss) / 2
            outputs = (total_loss,) + outputs

        return outputs  # (loss), start_logits, end_logits, (hidden_states), (attentions)
