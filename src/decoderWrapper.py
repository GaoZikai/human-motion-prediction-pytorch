from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from six.moves import xrange  # pylint: disable=redefined-builtin

import torch
import torch.nn as nn
from helper import *

class DecoderWrapper(nn.Module):
    def __init__(self,
                 cell,
                 rnn_size,
                 output_size,
                 target_seq_len,
                 residual_velocities,
                 device,
                 dtype=torch.float32):
        super(DecoderWrapper, self).__init__()
        self._cell = cell
        self.rnn_size = rnn_size
        self.output_size = output_size
        self.target_seq_len = target_seq_len
        self.residual = residual_velocities
        self.dtype= dtype
        self.device = device
        self.linear = nn.Linear(self.rnn_size, self.output_size)
        #Initial the linear op
        torch.nn.init.uniform_(self.linear.weight, -0.04 , 0.04)

    def forward(self,input,state):
        output = torch.zeros(self.target_seq_len,input.shape[1], input.shape[2] ,requires_grad=False, dtype=self.dtype).to(self.device)
        for i in xrange(self.target_seq_len):
            temp, state = self._cell(input, state)
            new_frame = input.clone()
            new_frame[:,:,:self.output_size] = self.linear(temp) + input[:,:,:self.output_size] if self.residual else self.linear(temp)
            output[i] = new_frame
        return output, state

# Mean and std are seq * batch * 54 , sample is seq * batch * input_size
class StochasticDecoderWrapper(nn.Module):
    def __init__(self,
                 cell,
                 rnn_size,
                 # inter_dim,
                 output_size,
                 target_seq_len,
                 residual_velocities,
                 device,
                 dtype=torch.float32):
        super(StochasticDecoderWrapper, self).__init__()
        self._cell = cell
        self.rnn_size = rnn_size
        # self.inter_dim = inter_dim
        self.output_size = output_size
        self.residual = residual_velocities
        self.target_seq_len = target_seq_len
        self.device = device
        self.dtype = dtype
        # self.init_dec = nn.Sequential(
        #                      nn.Linear(self.rnn_size, self.inter_dim),
        #                      nn.ReLU())
        # self.init_mean = nn.Linear(self.rnn_size, self.output_size)
        # self.dec = nn.Sequential(
        #                      nn.Linear(self.rnn_size, self.inter_dim),
        #                      nn.ReLU())
        self.mean = nn.Linear(self.rnn_size, self.output_size)
        self.std = nn.Sequential(
            nn.Linear(self.rnn_size, self.output_size),
            nn.Softplus())
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.uniform_(m.weight, -0.05, 0.05)


    def forward(self, input, state):
        output_mean = torch.zeros(self.target_seq_len,input.shape[1], self.output_size ,requires_grad=False, dtype=self.dtype).to(self.device)
        output_std = torch.zeros(self.target_seq_len,input.shape[1], self.output_size ,requires_grad=False, dtype=self.dtype).to(self.device)
        output_sample = torch.zeros(self.target_seq_len,input.shape[1], input.shape[2] ,requires_grad=False, dtype=self.dtype).to(self.device)
        for i in xrange(self.target_seq_len):
            temp, state = self._cell(input, state)
            mean = self.mean(temp)
            std =  self.std(temp)
            next_frame = input.clone()
            next_frame[:,:,:self.output_size] = reparam_sample_gauss(mean, std) + input[:,:,:self.output_size] if self.residual else reparam_sample_gauss(mean, std)
            output_mean[i] = mean
            output_std[i] = std
            output_sample[i] = next_frame
            input = next_frame
        return output_mean, output_std, output_sample, state
