"""A GNN encoder and decider model for human motion prediction in pytorch"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import random

import numpy as np
import os
from six.moves import xrange  # pylint: disable=redefined-builtin
import data_utils
import decoderWrapper
from helper import *
import torch
import torch.nn as nn
import torch.nn.functional as F
from itertools import chain

# two basic operation for node and edge conversion
def node2edge(x, rec_encode, send_encode):
    recs = torch.matmul(rec_encode, x)
    sends = torch.matmul(send_encode, x)
    edges = torch.cat([sends, recs], dim=-1)
    return edges

def edge2node(x, rec_encode, send_encode, edge_weight):
    x = x * edge_weight
    incomming = torch.matmul(rec_encode.t(), x)
    weight_norm = torch.matmul(rec_encode.t(), edge_weight)
    return incomming / weight_norm

class GNNModel(nn.Module):
    def __init__(self,
        source_seq_len,
        target_seq_len,
        edge_hidden_dim,
        edge_out_dim,
        node_hidden_dim,
        node_out_dim,
        num_passing,
        rec_encode,
        send_encode,
        batch_size,
        number_of_actions,
        device,
        weight_share=True,
        do_prob=0.0,
        one_hot=False,
        residual_velocities=False,
        stochastic=False,
        mask=None,
        dtype=torch.float32
    ):
        super(GNNModel, self).__init__()
        self.NUM_JOINT = 21
        self.HUMAN_SIZE = self.NUM_JOINT * 3
        self.JOINT_DIM = 3 * (source_seq_len-1)
        self.encoder_input_size= self.JOINT_DIM + number_of_actions if one_hot else  self.JOINT_DIM
        self.decoder_input_size = 3 + number_of_actions if one_hot else 3

        # initial input size, to be altered.
        self.input_size = self.HUMAN_SIZE + number_of_actions if one_hot else self.HUMAN_SIZE
        print("One hot is ", one_hot)
        print("Encoder input size is %d" % self.encoder_input_size)
        print("decoder input size is %d" % self.decoder_input_size)

        self.source_seq_len = source_seq_len
        self.target_seq_len = target_seq_len
        self.edge_hidden_dim = edge_hidden_dim
        self.edge_out_dim = edge_out_dim
        self.node_hidden_dim = node_hidden_dim
        self.node_out_dim = node_out_dim
        self.batch_size = batch_size
        self.stochastic = stochastic
        self.rec_encode = rec_encode
        self.send_encode = send_encode
        self.one_hot = one_hot
        self.device = device
        self.dtype = dtype

        #build mask for the task
        self.mask = mask

        # declare the encoder, it outputs (batch_size * NUM_JOINT * node_hidden_dim)
        self.encoder = GNNEncoder(
            self.encoder_input_size,
            self.node_hidden_dim,
            self.node_out_dim,
            self.edge_hidden_dim,
            self.edge_out_dim,
            self.rec_encode,
            self.send_encode,
            num_passing,
            weight_share,
            do_prob,
        )

        core_decoder = RGNN(
            self.decoder_input_size,
            self.node_hidden_dim,
            self.node_out_dim,
            self.edge_hidden_dim,
            self.edge_out_dim,
            self.rec_encode,
            self.send_encode,
            num_passing,
            do_prob,
            self.dtype,
        )

        if not stochastic:
            self.decoder = decoderWrapper.DecoderWrapper(
                core_decoder,
                self.node_out_dim,
                # 3d coordinate or 3d angle,
                3,
                self.target_seq_len,
                residual_velocities,
                device,
                self.mask,
                dtype
            )
        else:
            self.decoder = decoderWrapper.StochasticDecoderWrapper(
                core_decoder,
                self.node_out_dim,
                3,
                self.target_seq_len,
                residual_velocities,
                device,
                self.mask,
                dtype
            )

    # considering the time value, mean should be seg_len * batch_size * x
    def loss(self,means, logstd, target, index_to_cal):
        # if self.loss_weight is None:
        # return nll_gauss(means, logstd, target)
        # return torch.abs((means-target)[:,:,index_to_cal]).sum()
        return nn.MSELoss(reduction='mean')(means[:,:,index_to_cal], target[:,:,index_to_cal])

    # encoder input should be batch_size * num_joint * input_size
    # decoder input should be target_seq_len * batch_size * num_joint * (3 + one_hot)
    def forward(self, encoder_input, decoder_input):
        node_hidden = self.encoder(encoder_input)
        # node_hidden = None
        if not self.stochastic:
            output, state = self.decoder(decoder_input, node_hidden)
            return output, state
        else:
            means, stds, samples, states = self.decoder(decoder_input, node_hidden)
            return means, stds, samples, states

    # get batch functions according to the seq2seq model, reformating the data
    def get_batch( self, data, actions, original_format=False, validation=False):
        """Get a random batch of data from the specified bucket, prepare for step.

        Args
          data: a list of sequences of size n-by-d to fit the model to.
          actions: a list of the actions we are using
        Returns
          The tuple (encoder_inputs, decoder_inputs, decoder_outputs);
          the constructed batches have the proper format to call step(...) later.
          encoder_input : batch_size * num_joint * (seq_len * 3)
          decoder_input : batch_size * seq_len * num_joint * 3
          decoder_output : batch_size * seq_len * input_size
        """

        if original_format:
            return self.get_batch_original(data, actions)
        all_keys    = list(data.keys())
        if validation:
            chosen_keys = list(range(0,30,2))
        # Select entries at random
        else:
            chosen_keys = np.random.choice( len(all_keys), self.batch_size )
        # chosen_keys = [0] * self.batch_size
        # How many frames in total do we need?
        total_frames = self.source_seq_len + self.target_seq_len
        batch_size = len(chosen_keys)

        encoder_inputs  = np.zeros((batch_size, self.NUM_JOINT, self.encoder_input_size), dtype=float)
        decoder_inputs  = np.zeros((batch_size, self.target_seq_len, self.NUM_JOINT, self.decoder_input_size), dtype=float)
        decoder_outputs = np.zeros((batch_size, self.target_seq_len, self.HUMAN_SIZE), dtype=float)

        encoder_inputs_ori  = np.zeros((batch_size, self.source_seq_len-1, self.input_size), dtype=float)
        decoder_inputs_ori  = np.zeros((batch_size, self.target_seq_len, self.input_size), dtype=float)

        for i in xrange( batch_size ):

          the_key = all_keys[ chosen_keys[i] ]

          # Get the number of frames
          n, _ = data[ the_key ].shape

          # Sample somewherein the middle
          if validation:
              idx = 17
          else:
              idx = np.random.randint( 16, n-total_frames )
          # idx = 17

          # Select the data around the sampled points
          data_sel = data[ the_key ][idx:idx+total_frames ,:]

          data_to_transform = data_sel
          # ugly replicate
          encoder_inputs_ori[i,:,0:self.input_size]  = data_sel[0:self.source_seq_len-1, :]
          decoder_inputs_ori[i,:,0:self.input_size]  = data_sel[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :]

          # get the class info
          if self.one_hot:
              class_encoding = data_sel[0,-len(actions):]
              data_to_transform = data_sel[:,:-len(actions)]
              data_sel = data_sel[:,:-len(actions)]
              encoder_inputs[i,:,-len(actions):] = class_encoding
              decoder_inputs[i,:,:,-len(actions):] = class_encoding
          # do the transfrom
          encoder_input = np.reshape(
            data_to_transform[0:self.source_seq_len-1,:], [-1, self.NUM_JOINT, 3])
          encoder_input =  np.reshape(np.transpose(encoder_input, [1,0,2]),[self.NUM_JOINT,-1])
          decoder_input = np.reshape(
            data_to_transform[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :],
            [-1, self.NUM_JOINT, 3]
          )
          decoder_output = data_to_transform[self.source_seq_len:, :]

          # Add the data
          encoder_inputs[i,:,0:self.JOINT_DIM]  = encoder_input
          decoder_inputs[i,:,:,0:3]  = decoder_input
          decoder_outputs[i,:,0:self.HUMAN_SIZE] = decoder_output


          # alter data to expected form
        encoder_inputs = torch.tensor(encoder_inputs,dtype=self.dtype).to(self.device)
        decoder_inputs = torch.tensor(decoder_inputs,dtype=self.dtype).permute(1,0,2,3).to(self.device)
        decoder_outputs = torch.tensor(decoder_outputs,dtype=self.dtype).permute(1,0,2).to(self.device)

        transform = lambda x: torch.tensor(x, dtype=self.dtype).permute(1,0,2).to(self.device)
        encoder_inputs_ori = transform(encoder_inputs_ori)
        decoder_inputs_ori = transform(decoder_inputs_ori)

        return encoder_inputs, decoder_inputs, decoder_outputs, encoder_inputs_ori, decoder_inputs_ori


    def find_indices_srnn( self, data, action, original_format=False):
        """
        Find the same action indices as in SRNN.
        See https://github.com/asheshjain399/RNNexp/blob/master/structural_rnn/CRFProblems/H3.6m/processdata.py#L325
        """

        # Used a fixed dummy seed, following
        # https://github.com/asheshjain399/RNNexp/blob/srnn/structural_rnn/forecastTrajectories.py#L29
        SEED = 1234567890
        rng = np.random.RandomState( SEED )

        subject = 5
        subaction1 = 1
        subaction2 = 2

        T1 = data[ (subject, action, subaction1, 'even') ].shape[0]
        T2 = data[ (subject, action, subaction2, 'even') ].shape[0]
        prefix, suffix = 50, 100

        idx = []
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        return idx

    def get_batch_srnn(self, data, action):
        """
        Get a random batch of data from the specified bucket, prepare for step.

        Args
          data: dictionary with k:v, k=((subject, action, subsequence, 'even')),
            v=nxd matrix with a sequence of poses
          action: the action to load data from
        Returns
          The tuple (encoder_inputs, decoder_inputs, decoder_outputs);
          the constructed batches have the proper format to call step(...) later.
        """

        actions = ["directions", "discussion", "eating", "greeting", "phoning",
                  "posing", "purchases", "sitting", "sittingdown", "smoking",
                  "takingphoto", "waiting", "walking", "walkingdog", "walkingtogether"]

        if not action in actions:
          raise ValueError("Unrecognized action {0}".format(action))

        frames = {}
        frames[ action ] = self.find_indices_srnn( data, action )

        batch_size = 8 # we always evaluate 8 seeds
        subject    = 5 # we always evaluate on subject 5
        source_seq_len = self.source_seq_len
        target_seq_len = self.target_seq_len

        seeds = [( action, (i%2)+1, frames[action][i] ) for i in range(batch_size)]

        encoder_inputs  = np.zeros((batch_size, self.NUM_JOINT, self.encoder_input_size), dtype=float)
        decoder_inputs  = np.zeros((batch_size, self.target_seq_len, self.NUM_JOINT, self.decoder_input_size), dtype=float)
        decoder_outputs = np.zeros((batch_size, self.target_seq_len, self.HUMAN_SIZE), dtype=float)

        # Compute the number of frames needed
        total_frames = source_seq_len + target_seq_len

        # Reproducing SRNN's sequence subsequence selection as done in
        # https://github.com/asheshjain399/RNNexp/blob/master/structural_rnn/CRFProblems/H3.6m/processdata.py#L343
        for i in xrange( batch_size ):

          _, subsequence, idx = seeds[i]
          idx = idx + 50

          data_sel = data[ (subject, action, subsequence, 'even') ]

          data_sel = data_sel[(idx-source_seq_len):(idx+target_seq_len) ,:]

          # add transform to coordinate

          data_to_transform = data_sel
          # get the class info
          if self.one_hot:
              class_encoding = data_sel[0,-len(actions):]
              data_to_transform = data_sel[:,:-len(actions)]
              data_sel = data_sel[:,:-len(actions)]
              encoder_inputs[i,:,-len(actions):] = class_encoding
              decoder_inputs[i,:,:,-len(actions):] = class_encoding
          # do the transfrom
          encoder_input = np.reshape(
            data_to_transform[0:self.source_seq_len-1,:], [-1, self.NUM_JOINT, 3])
          encoder_input =  np.reshape(np.transpose(encoder_input, [1,0,2]),[self.NUM_JOINT,-1])
          decoder_input = np.reshape(
            data_to_transform[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :],
            [-1, self.NUM_JOINT, 3]
          )
          decoder_output = data_to_transform[self.source_seq_len:, :]
          # Add the data
          encoder_inputs[i,:,0:self.JOINT_DIM]  = encoder_input
          decoder_inputs[i,:,:,0:3]  = decoder_input
          decoder_outputs[i,:,0:self.HUMAN_SIZE] = decoder_output

        encoder_inputs = torch.tensor(encoder_inputs,dtype=self.dtype).to(self.device)
        decoder_inputs = torch.tensor(decoder_inputs,dtype=self.dtype).permute(1,0,2,3).to(self.device)
        decoder_outputs = torch.tensor(decoder_outputs,dtype=self.dtype).permute(1,0,2).to(self.device)

        return encoder_inputs, decoder_inputs, decoder_outputs

class GNNModel2(nn.Module):
    def __init__(self,
        source_seq_len,
        target_seq_len,
        edge_hidden_dim,
        edge_out_dim,
        node_hidden_dim,
        node_out_dim,
        num_passing,
        rec_encode,
        send_encode,
        batch_size,
        number_of_actions,
        device,
        weight_share=True,
        do_prob=0.0,
        one_hot=False,
        residual_velocities=False,
        stochastic=False,
        mask=None,
        dtype=torch.float32
    ):
        super(GNNModel2, self).__init__()
        self.NUM_JOINT = 21
        self.HUMAN_SIZE = self.NUM_JOINT * 3
        self.JOINT_DIM = 3 * source_seq_len
        self.encoder_input_size= self.JOINT_DIM + number_of_actions if one_hot else  self.JOINT_DIM
        self.decoder_input_size = 3 + number_of_actions if one_hot else 3

        # initial input size, to be altered.
        self.input_size = self.HUMAN_SIZE + number_of_actions if one_hot else self.HUMAN_SIZE
        print("One hot is ", one_hot)
        print("Encoder input size is %d" % self.encoder_input_size)
        print("decoder input size is %d" % self.decoder_input_size)

        self.source_seq_len = source_seq_len
        self.target_seq_len = target_seq_len
        self.edge_hidden_dim = edge_hidden_dim
        self.edge_out_dim = edge_out_dim
        self.node_hidden_dim = node_hidden_dim
        self.node_out_dim = node_out_dim
        self.batch_size = batch_size
        self.stochastic = stochastic
        self.rec_encode = rec_encode
        self.send_encode = send_encode
        self.one_hot = one_hot
        self.device = device
        self.dtype = dtype

        #build mask for the task
        self.mask = mask

        # declare the encoder, it outputs (batch_size * NUM_JOINT * node_hidden_dim)
        self.encoder = GNNEncoder(
            self.encoder_input_size,
            self.node_hidden_dim,
            self.node_out_dim,
            self.edge_hidden_dim,
            self.edge_out_dim,
            self.rec_encode,
            self.send_encode,
            num_passing,
            weight_share,
            do_prob,
        )

        core_decoder = RGNN(
            self.decoder_input_size,
            self.node_hidden_dim,
            self.node_out_dim,
            self.edge_hidden_dim,
            self.edge_out_dim,
            self.rec_encode,
            self.send_encode,
            num_passing,
            do_prob,
            self.dtype,
        )

        if not stochastic:
            self.decoder = decoderWrapper.DecoderWrapper(
                core_decoder,
                self.node_out_dim,
                # 3d coordinate or 3d angle,
                3,
                self.target_seq_len,
                residual_velocities,
                device,
                self.mask,
                dtype
            )
        else:
            self.decoder = decoderWrapper.StochasticDecoderWrapper(
                core_decoder,
                self.node_out_dim,
                3,
                self.target_seq_len,
                residual_velocities,
                device,
                self.mask,
                dtype
            )

    # considering the time value, mean should be seg_len * batch_size * x
    def loss(self,means, logstd, target, index_to_cal):
        # if self.loss_weight is None:
        # return nll_gauss(means, logstd, target)
        # return nll_gauss(means[:,:,index_to_cal], logstd[:,:,index_to_cal], target[:,:,index_to_cal])
        # return torch.abs((means-target)[:,:,index_to_cal]).sum()
        return nn.MSELoss(reduction='mean')(means[:,:,index_to_cal], target[:,:,index_to_cal])

    # encoder input should be batch_size * num_joint * input_size
    # decoder input should be target_seq_len * batch_size * num_joint * (3 + one_hot)
    def forward(self, encoder_input, decoder_input):
        node_hidden = self.encoder(encoder_input)
        # node_hidden = None
        if not self.stochastic:
            output, state = self.decoder(decoder_input, node_hidden)
            return output, state
        else:
            means, stds, samples, states = self.decoder(decoder_input, node_hidden)
            return means, stds, samples, states

    # get batch functions according to the seq2seq model, reformating the data
    def get_batch( self, data, actions, original_format=False, validation=False):
        """Get a random batch of data from the specified bucket, prepare for step.

        Args
          data: a list of sequences of size n-by-d to fit the model to.
          actions: a list of the actions we are using
        Returns
          The tuple (encoder_inputs, decoder_inputs, decoder_outputs);
          the constructed batches have the proper format to call step(...) later.
          encoder_input : batch_size * num_joint * (seq_len * 3)
          decoder_input : batch_size * seq_len * num_joint * 3
          decoder_output : batch_size * seq_len * input_size
        """

        if original_format:
            return self.get_batch_original(data, actions)
        all_keys    = list(data.keys())
        if validation:
            chosen_keys = list(range(0,30,2))
        # Select entries at random
        else:
            chosen_keys = np.random.choice( len(all_keys), self.batch_size )
        # chosen_keys = [0] * self.batch_size
        # How many frames in total do we need?
        total_frames = self.source_seq_len + self.target_seq_len
        batch_size = len(chosen_keys)

        encoder_inputs  = np.zeros((batch_size, self.NUM_JOINT, self.encoder_input_size), dtype=float)
        decoder_inputs  = np.zeros((batch_size, self.target_seq_len, self.NUM_JOINT, self.decoder_input_size), dtype=float)
        decoder_outputs = np.zeros((batch_size, self.target_seq_len, self.HUMAN_SIZE), dtype=float)

        encoder_inputs_ori  = np.zeros((batch_size, self.source_seq_len, self.input_size), dtype=float)
        decoder_inputs_ori  = np.zeros((batch_size, self.target_seq_len, self.input_size), dtype=float)

        for i in xrange( batch_size ):

          the_key = all_keys[ chosen_keys[i] ]

          # Get the number of frames
          n, _ = data[ the_key ].shape

          # Sample somewherein the middle
          if validation:
              idx = 17
          else:
              idx = np.random.randint( 16, n-total_frames )
          # idx = 17

          # Select the data around the sampled points
          data_sel = data[ the_key ][idx:idx+total_frames ,:]
          data_to_transform = data_sel
          # ugly replicate
          encoder_inputs_ori[i,:,0:self.input_size]  = data_sel[0:self.source_seq_len, :]
          decoder_inputs_ori[i,:,0:self.input_size]  = data_sel[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :]

          # get the class info
          if self.one_hot:
              class_encoding = data_sel[0,-len(actions):]
              data_to_transform = data_sel[:,:-len(actions)]
              data_sel = data_sel[:,:-len(actions)]
              encoder_inputs[i,:,-len(actions):] = class_encoding
              decoder_inputs[i,:,:,-len(actions):] = class_encoding
          # do the transfrom
          encoder_input = np.reshape(
            data_to_transform[0:self.source_seq_len,:], [-1, self.NUM_JOINT, 3])
          encoder_input =  np.reshape(np.transpose(encoder_input, [1,0,2]),[self.NUM_JOINT,-1])
          decoder_input = np.reshape(
            data_to_transform[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :],
            [-1, self.NUM_JOINT, 3]
          )
          decoder_output = data_to_transform[self.source_seq_len:, :]

          # Add the data
          encoder_inputs[i,:,0:self.JOINT_DIM]  = encoder_input
          decoder_inputs[i,:,:,0:3]  = decoder_input
          decoder_outputs[i,:,0:self.HUMAN_SIZE] = decoder_output
          # alter data to expected form
        encoder_inputs = torch.tensor(encoder_inputs,dtype=self.dtype).to(self.device)
        decoder_inputs = torch.tensor(decoder_inputs,dtype=self.dtype).permute(1,0,2,3).to(self.device)
        decoder_outputs = torch.tensor(decoder_outputs,dtype=self.dtype).permute(1,0,2).to(self.device)

        transform = lambda x: torch.tensor(x, dtype=self.dtype).permute(1,0,2).to(self.device)
        encoder_inputs_ori = transform(encoder_inputs_ori)
        decoder_inputs_ori = transform(decoder_inputs_ori)

        return encoder_inputs, decoder_inputs, decoder_outputs, encoder_inputs_ori, decoder_inputs_ori


    def find_indices_srnn( self, data, action, original_format=False):
        """
        Find the same action indices as in SRNN.
        See https://github.com/asheshjain399/RNNexp/blob/master/structural_rnn/CRFProblems/H3.6m/processdata.py#L325
        """

        # Used a fixed dummy seed, following
        # https://github.com/asheshjain399/RNNexp/blob/srnn/structural_rnn/forecastTrajectories.py#L29
        SEED = 1234567890
        rng = np.random.RandomState( SEED )

        subject = 5
        subaction1 = 1
        subaction2 = 2

        T1 = data[ (subject, action, subaction1, 'even') ].shape[0]
        T2 = data[ (subject, action, subaction2, 'even') ].shape[0]
        prefix, suffix = 50, 100

        idx = []
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        return idx

    def get_batch_srnn(self, data, action):
        """
        Get a random batch of data from the specified bucket, prepare for step.

        Args
          data: dictionary with k:v, k=((subject, action, subsequence, 'even')),
            v=nxd matrix with a sequence of poses
          action: the action to load data from
        Returns
          The tuple (encoder_inputs, decoder_inputs, decoder_outputs);
          the constructed batches have the proper format to call step(...) later.
        """

        actions = ["directions", "discussion", "eating", "greeting", "phoning",
                  "posing", "purchases", "sitting", "sittingdown", "smoking",
                  "takingphoto", "waiting", "walking", "walkingdog", "walkingtogether"]

        if not action in actions:
          raise ValueError("Unrecognized action {0}".format(action))

        frames = {}
        frames[ action ] = self.find_indices_srnn( data, action )

        batch_size = 8 # we always evaluate 8 seeds
        subject    = 5 # we always evaluate on subject 5
        source_seq_len = self.source_seq_len
        target_seq_len = self.target_seq_len

        seeds = [( action, (i%2)+1, frames[action][i] ) for i in range(batch_size)]

        encoder_inputs  = np.zeros((batch_size, self.NUM_JOINT, self.encoder_input_size), dtype=float)
        decoder_inputs  = np.zeros((batch_size, self.target_seq_len, self.NUM_JOINT, self.decoder_input_size), dtype=float)
        decoder_outputs = np.zeros((batch_size, self.target_seq_len, self.HUMAN_SIZE), dtype=float)

        # Compute the number of frames needed
        total_frames = source_seq_len + target_seq_len

        # Reproducing SRNN's sequence subsequence selection as done in
        # https://github.com/asheshjain399/RNNexp/blob/master/structural_rnn/CRFProblems/H3.6m/processdata.py#L343
        for i in xrange( batch_size ):

          _, subsequence, idx = seeds[i]
          idx = idx + 50

          data_sel = data[ (subject, action, subsequence, 'even') ]

          data_sel = data_sel[(idx-source_seq_len):(idx+target_seq_len) ,:]
          # add transform to coordinate

          data_to_transform = data_sel
          # get the class info
          if self.one_hot:
              class_encoding = data_sel[0,-len(actions):]
              data_to_transform = data_sel[:,:-len(actions)]
              data_sel = data_sel[:,:-len(actions)]
              encoder_inputs[i,:,-len(actions):] = class_encoding
              decoder_inputs[i,:,:,-len(actions):] = class_encoding
          # do the transfrom
          encoder_input = np.reshape(
            data_to_transform[0:self.source_seq_len,:], [-1, self.NUM_JOINT, 3])
          encoder_input =  np.reshape(np.transpose(encoder_input, [1,0,2]),[self.NUM_JOINT,-1])
          decoder_input = np.reshape(
            data_to_transform[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :],
            [-1, self.NUM_JOINT, 3]
          )
          decoder_output = data_to_transform[self.source_seq_len:, :]
          # Add the data
          encoder_inputs[i,:,0:self.JOINT_DIM]  = encoder_input
          decoder_inputs[i,:,:,0:3]  = decoder_input
          decoder_outputs[i,:,0:self.HUMAN_SIZE] = decoder_output
        encoder_inputs = torch.tensor(encoder_inputs,dtype=self.dtype).to(self.device)
        decoder_inputs = torch.tensor(decoder_inputs,dtype=self.dtype).permute(1,0,2,3).to(self.device)
        decoder_outputs = torch.tensor(decoder_outputs,dtype=self.dtype).permute(1,0,2).to(self.device)

        return encoder_inputs, decoder_inputs, decoder_outputs

class GNNModel3(nn.Module):
    def __init__(self,
        source_seq_len,
        target_seq_len,
        edge_hidden_dim,
        edge_out_dim,
        node_hidden_dim,
        node_out_dim,
        num_passing,
        rec_encode,
        send_encode,
        batch_size,
        number_of_actions,
        device,
        weight_share=False,
        do_prob=0.0,
        one_hot=False,
        residual_velocities=False,
        stochastic=False,
        mask=None,
        dtype=torch.float32
    ):
        super(GNNModel3, self).__init__()
        self.NUM_JOINT = 21
        self.HUMAN_SIZE = self.NUM_JOINT * 3
        self.JOINT_DIM = 3 * source_seq_len
        self.encoder_input_size= self.JOINT_DIM + number_of_actions if one_hot else  self.JOINT_DIM
        self.decoder_input_size = 3 + number_of_actions if one_hot else 3

        # initial input size, to be altered.
        self.input_size = self.HUMAN_SIZE + number_of_actions if one_hot else self.HUMAN_SIZE
        print("One hot is ", one_hot)
        print("Encoder input size is %d" % self.encoder_input_size)
        print("decoder input size is %d" % self.decoder_input_size)

        self.source_seq_len = source_seq_len
        self.target_seq_len = target_seq_len
        self.edge_hidden_dim = edge_hidden_dim
        self.edge_out_dim = edge_out_dim
        self.node_hidden_dim = node_hidden_dim
        self.node_out_dim = node_out_dim
        self.batch_size = batch_size
        self.stochastic = stochastic
        self.rec_encode = rec_encode
        self.send_encode = send_encode
        self.one_hot = one_hot
        self.device = device
        self.dtype = dtype

        #build mask for the task
        self.mask = mask

        # transform original node feature to node embedding
        self.mlp1 = MLP(self.encoder_input_size, self.node_hidden_dim, self.node_out_dim)

        # declare the encoder, it outputs (batch_size * NUM_JOINT * node_hidden_dim)
        self.encoder = GNNEncoder(
            self.node_hidden_dim,
            self.node_out_dim,
            self.edge_hidden_dim,
            self.edge_out_dim,
            self.rec_encode,
            self.send_encode,
            num_passing+1,
            weight_share,
            do_prob,
        )

        core_decoder = RGNN(
            self.decoder_input_size,
            self.node_hidden_dim,
            self.node_out_dim,
            self.edge_hidden_dim,
            self.edge_out_dim,
            self.rec_encode,
            self.send_encode,
            num_passing,
            do_prob,
            self.dtype,
        )

        if not stochastic:
            self.decoder = decoderWrapper.DecoderWrapper(
                core_decoder,
                self.node_out_dim,
                # 3d coordinate or 3d angle,
                3,
                self.target_seq_len,
                residual_velocities,
                device,
                self.mask,
                dtype
            )
        else:
            self.decoder = decoderWrapper.StochasticDecoderWrapper2(
                core_decoder,
                self.node_out_dim,
                3,
                self.target_seq_len,
                residual_velocities,
                device,
                self.node_hidden_dim,
                self.node_out_dim,
                self.edge_hidden_dim,
                self.node_out_dim,
                self.rec_encode,
                self.send_encode,
                num_passing,
                do_prob,
                self.mask,
                dtype,
            )

    # considering the time value, mean should be seg_len * batch_size * x
    def loss(self,means, logstd, target, index_to_cal):
        # if self.loss_weight is None:
        # return nll_gauss(means, logstd, target)
        # return nll_gauss(means[:,:,index_to_cal], logstd[:,:,index_to_cal], target[:,:,index_to_cal])
        return torch.abs((means-target)[:,:,index_to_cal]).sum()
        # return nn.MSELoss(reduction='sum')(means[:,:,index_to_cal], target[:,:,index_to_cal])

    # encoder input should be batch_size * num_joint * input_size
    # decoder input should be target_seq_len * batch_size * num_joint * (3 + one_hot)
    def forward(self, encoder_input, decoder_input):
        node_hidden = self.mlp1(encoder_input)
        node_hidden, edge_weight = self.encoder(node_hidden, None)

        # node_hidden = None
        if not self.stochastic:
            output, state = self.decoder(decoder_input, node_hidden,edge_weight)
            return output, state
        else:
            means, stds, samples, states, edge_weight= self.decoder(decoder_input, node_hidden,edge_weight)
            return means, stds, samples, states, edge_weight

    # get batch functions according to the seq2seq model, reformating the data
    def get_batch( self, data, actions, original_format=False, validation=False):
        """Get a random batch of data from the specified bucket, prepare for step.

        Args
          data: a list of sequences of size n-by-d to fit the model to.
          actions: a list of the actions we are using
        Returns
          The tuple (encoder_inputs, decoder_inputs, decoder_outputs);
          the constructed batches have the proper format to call step(...) later.
          encoder_input : batch_size * num_joint * (seq_len * 3)
          decoder_input : batch_size * seq_len * num_joint * 3
          decoder_output : batch_size * seq_len * input_size
        """

        if original_format:
            return self.get_batch_original(data, actions)
        all_keys    = list(data.keys())
        if validation:
            chosen_keys = list(range(0,30,2))
        # Select entries at random
        else:
            chosen_keys = np.random.choice( len(all_keys), self.batch_size )
        # chosen_keys = [0] * self.batch_size
        # How many frames in total do we need?
        total_frames = self.source_seq_len + self.target_seq_len
        batch_size = len(chosen_keys)

        encoder_inputs  = np.zeros((batch_size, self.NUM_JOINT, self.encoder_input_size), dtype=float)
        decoder_inputs  = np.zeros((batch_size, self.target_seq_len, self.NUM_JOINT, self.decoder_input_size), dtype=float)
        decoder_outputs = np.zeros((batch_size, self.target_seq_len, self.HUMAN_SIZE), dtype=float)

        encoder_inputs_ori  = np.zeros((batch_size, self.source_seq_len, self.input_size), dtype=float)
        decoder_inputs_ori  = np.zeros((batch_size, self.target_seq_len, self.input_size), dtype=float)

        for i in xrange( batch_size ):

          the_key = all_keys[ chosen_keys[i] ]

          # Get the number of frames
          n, _ = data[ the_key ].shape

          # Sample somewherein the middle
          if validation:
              idx = 17
          else:
              idx = np.random.randint( 16, n-total_frames )
          # idx = 17

          # Select the data around the sampled points
          data_sel = data[ the_key ][idx:idx+total_frames ,:]
          data_to_transform = data_sel
          # ugly replicate
          encoder_inputs_ori[i,:,0:self.input_size]  = data_sel[0:self.source_seq_len, :]
          decoder_inputs_ori[i,:,0:self.input_size]  = data_sel[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :]

          # get the class info
          if self.one_hot:
              class_encoding = data_sel[0,-len(actions):]
              data_to_transform = data_sel[:,:-len(actions)]
              data_sel = data_sel[:,:-len(actions)]
              encoder_inputs[i,:,-len(actions):] = class_encoding
              decoder_inputs[i,:,:,-len(actions):] = class_encoding
          # do the transfrom
          encoder_input = np.reshape(
            data_to_transform[0:self.source_seq_len,:], [-1, self.NUM_JOINT, 3])
          encoder_input =  np.reshape(np.transpose(encoder_input, [1,0,2]),[self.NUM_JOINT,-1])
          decoder_input = np.reshape(
            data_to_transform[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :],
            [-1, self.NUM_JOINT, 3]
          )
          decoder_output = data_to_transform[self.source_seq_len:, :]

          # Add the data
          encoder_inputs[i,:,0:self.JOINT_DIM]  = encoder_input
          decoder_inputs[i,:,:,0:3]  = decoder_input
          decoder_outputs[i,:,0:self.HUMAN_SIZE] = decoder_output
          # alter data to expected form
        encoder_inputs = torch.tensor(encoder_inputs,dtype=self.dtype).to(self.device)
        decoder_inputs = torch.tensor(decoder_inputs,dtype=self.dtype).permute(1,0,2,3).to(self.device)
        decoder_outputs = torch.tensor(decoder_outputs,dtype=self.dtype).permute(1,0,2).to(self.device)

        transform = lambda x: torch.tensor(x, dtype=self.dtype).permute(1,0,2).to(self.device)
        encoder_inputs_ori = transform(encoder_inputs_ori)
        decoder_inputs_ori = transform(decoder_inputs_ori)

        return encoder_inputs, decoder_inputs, decoder_outputs, encoder_inputs_ori, decoder_inputs_ori


    def find_indices_srnn( self, data, action, original_format=False):
        """
        Find the same action indices as in SRNN.
        See https://github.com/asheshjain399/RNNexp/blob/master/structural_rnn/CRFProblems/H3.6m/processdata.py#L325
        """

        # Used a fixed dummy seed, following
        # https://github.com/asheshjain399/RNNexp/blob/srnn/structural_rnn/forecastTrajectories.py#L29
        SEED = 1234567890
        rng = np.random.RandomState( SEED )

        subject = 5
        subaction1 = 1
        subaction2 = 2

        T1 = data[ (subject, action, subaction1, 'even') ].shape[0]
        T2 = data[ (subject, action, subaction2, 'even') ].shape[0]
        prefix, suffix = 50, 100

        idx = []
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        return idx

    def get_batch_srnn(self, data, action):
        """
        Get a random batch of data from the specified bucket, prepare for step.

        Args
          data: dictionary with k:v, k=((subject, action, subsequence, 'even')),
            v=nxd matrix with a sequence of poses
          action: the action to load data from
        Returns
          The tuple (encoder_inputs, decoder_inputs, decoder_outputs);
          the constructed batches have the proper format to call step(...) later.
        """

        actions = ["directions", "discussion", "eating", "greeting", "phoning",
                  "posing", "purchases", "sitting", "sittingdown", "smoking",
                  "takingphoto", "waiting", "walking", "walkingdog", "walkingtogether"]

        if not action in actions:
          raise ValueError("Unrecognized action {0}".format(action))

        frames = {}
        frames[ action ] = self.find_indices_srnn( data, action )

        batch_size = 8 # we always evaluate 8 seeds
        subject    = 5 # we always evaluate on subject 5
        source_seq_len = self.source_seq_len
        target_seq_len = self.target_seq_len

        seeds = [( action, (i%2)+1, frames[action][i] ) for i in range(batch_size)]

        encoder_inputs  = np.zeros((batch_size, self.NUM_JOINT, self.encoder_input_size), dtype=float)
        decoder_inputs  = np.zeros((batch_size, self.target_seq_len, self.NUM_JOINT, self.decoder_input_size), dtype=float)
        decoder_outputs = np.zeros((batch_size, self.target_seq_len, self.HUMAN_SIZE), dtype=float)

        # Compute the number of frames needed
        total_frames = source_seq_len + target_seq_len

        # Reproducing SRNN's sequence subsequence selection as done in
        # https://github.com/asheshjain399/RNNexp/blob/master/structural_rnn/CRFProblems/H3.6m/processdata.py#L343
        for i in xrange( batch_size ):

          _, subsequence, idx = seeds[i]
          idx = idx + 50

          data_sel = data[ (subject, action, subsequence, 'even') ]

          data_sel = data_sel[(idx-source_seq_len):(idx+target_seq_len) ,:]
          # add transform to coordinate

          data_to_transform = data_sel
          # get the class info
          if self.one_hot:
              class_encoding = data_sel[0,-len(actions):]
              data_to_transform = data_sel[:,:-len(actions)]
              data_sel = data_sel[:,:-len(actions)]
              encoder_inputs[i,:,-len(actions):] = class_encoding
              decoder_inputs[i,:,:,-len(actions):] = class_encoding
          # do the transfrom
          encoder_input = np.reshape(
            data_to_transform[0:self.source_seq_len,:], [-1, self.NUM_JOINT, 3])
          encoder_input =  np.reshape(np.transpose(encoder_input, [1,0,2]),[self.NUM_JOINT,-1])
          decoder_input = np.reshape(
            data_to_transform[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :],
            [-1, self.NUM_JOINT, 3]
          )
          decoder_output = data_to_transform[self.source_seq_len:, :]
          # Add the data
          encoder_inputs[i,:,0:self.JOINT_DIM]  = encoder_input
          decoder_inputs[i,:,:,0:3]  = decoder_input
          decoder_outputs[i,:,0:self.HUMAN_SIZE] = decoder_output
        encoder_inputs = torch.tensor(encoder_inputs,dtype=self.dtype).to(self.device)
        decoder_inputs = torch.tensor(decoder_inputs,dtype=self.dtype).permute(1,0,2,3).to(self.device)
        decoder_outputs = torch.tensor(decoder_outputs,dtype=self.dtype).permute(1,0,2).to(self.device)

        return encoder_inputs, decoder_inputs, decoder_outputs


# GNN + RNN model
class RGNNModel(nn.Module):
    def __init__(self,
        source_seq_len,
        target_seq_len,
        edge_hidden_dim,
        edge_out_dim,
        node_hidden_dim,
        node_out_dim,
        num_passing,
        rec_encode,
        send_encode,
        batch_size,
        number_of_actions,
        device,
        tied=True,
        weight_share=False,
        do_prob=0.0,
        one_hot=False,
        residual_velocities=False,
        stochastic=False,
        mask=None,
        dtype=torch.float32
    ):
        super(RGNNModel, self).__init__()
        self.NUM_JOINT = 21
        self.HUMAN_SIZE = self.NUM_JOINT * 3
        self.input_size = 3 + number_of_actions if one_hot else 3
        self.ori_input_size = self.HUMAN_SIZE + number_of_actions if one_hot else self.HUMAN_SIZE
        print("One hot is ", one_hot)
        print("Encoder/Decoder input size is %d" % self.input_size)

        self.source_seq_len = source_seq_len
        self.target_seq_len = target_seq_len
        self.edge_hidden_dim = edge_hidden_dim
        self.edge_out_dim = edge_out_dim
        self.node_hidden_dim = node_hidden_dim
        self.node_out_dim = node_out_dim
        self.batch_size = batch_size
        self.stochastic = stochastic
        self.rec_encode = rec_encode
        self.send_encode = send_encode
        self.one_hot = one_hot
        self.device = device
        self.dtype = dtype

        #build mask for the task
        self.mask = mask

        # declare the encoder, it outputs (batch_size * NUM_JOINT * node_hidden_dim)
        self.encoder = RGNN(
            self.input_size,
            self.node_hidden_dim,
            self.node_out_dim,
            self.edge_hidden_dim,
            self.edge_out_dim,
            self.rec_encode,
            self.send_encode,
            num_passing,
            do_prob,
            self.dtype
        )

        if tied:
            core_decoder = self.encoder
        else:
            core_decoder = RGNN(
                self.input_size,
                self.node_hidden_dim,
                self.node_out_dim,
                self.edge_hidden_dim,
                self.edge_out_dim,
                self.rec_encode,
                self.send_encode,
                num_passing,
                do_prob,
                self.dtype,
            )

        if not stochastic:
            self.decoder = decoderWrapper.DecoderWrapper(
                core_decoder,
                self.node_out_dim,
                # 3d coordinate or 3d angle,
                3,
                self.target_seq_len,
                residual_velocities,
                device,
                self.mask,
                dtype
            )
        else:
            self.decoder = decoderWrapper.StochasticDecoderWrapper(
                core_decoder,
                self.node_out_dim,
                3,
                self.target_seq_len,
                residual_velocities,
                device,
                self.mask,
                dtype
            )

    # considering the time value, mean should be seg_len * batch_size * x
    def loss(self,means, logstd, target, index_to_cal):
        # if self.loss_weight is None:
        return nll_gauss(means[:,:,index_to_cal], logstd[:,:,index_to_cal], target[:,:,index_to_cal])
        # return torch.abs((means-target)[:,:,index_to_cal]).sum()
        # return nn.MSELoss(reduction='sum')(means[:,:,index_to_cal], target[:,:,index_to_cal])

    # encoder input should be batch_size * num_joint * input_size
    # decoder input should be target_seq_len * batch_size * num_joint * (3 + one_hot)
    def forward(self, encoder_input, decoder_input):
        _, node_hidden = self.encoder(encoder_input, None)
        if not self.stochastic:
            output, state = self.decoder(decoder_input, node_hidden)
            return output, state
        else:
            means, stds, samples, states = self.decoder(decoder_input, node_hidden)
            return means, stds, samples, states

    # get batch functions according to the seq2seq model, reformating the data
    def get_batch( self, data, actions, original_format=False, validation=False):
        """Get a random batch of data from the specified bucket, prepare for step.

        Args
          data: a list of sequences of size n-by-d to fit the model to.
          actions: a list of the actions we are using
        Returns
          The tuple (encoder_inputs, decoder_inputs, decoder_outputs);
          the constructed batches have the proper format to call step(...) later.
          encoder_input : batch_size * num_joint * (seq_len * 3)
          decoder_input : batch_size * seq_len * num_joint * 3
          decoder_output : batch_size * seq_len * input_size
        """

        if original_format:
            return self.get_batch_original(data, actions)
        all_keys    = list(data.keys())
        if validation:
            chosen_keys = list(range(0,30,2))
        # Select entries at random
        else:
            chosen_keys = np.random.choice( len(all_keys), self.batch_size )
        # chosen_keys = [0] * self.batch_size
        # How many frames in total do we need?
        total_frames = self.source_seq_len + self.target_seq_len
        batch_size = len(chosen_keys)

        encoder_inputs  = np.zeros((batch_size, self.source_seq_len-1, self.NUM_JOINT, self.input_size), dtype=float)
        decoder_inputs  = np.zeros((batch_size, self.target_seq_len, self.NUM_JOINT, self.input_size), dtype=float)
        decoder_outputs = np.zeros((batch_size, self.target_seq_len, self.HUMAN_SIZE), dtype=float)

        encoder_inputs_ori  = np.zeros((batch_size, self.source_seq_len-1, self.ori_input_size), dtype=float)
        decoder_inputs_ori  = np.zeros((batch_size, self.target_seq_len, self.ori_input_size), dtype=float)

        for i in xrange( batch_size ):

          the_key = all_keys[ chosen_keys[i] ]

          # Get the number of frames
          n, _ = data[ the_key ].shape

          # Sample somewherein the middle
          if validation:
              idx = 17
          else:
              idx = np.random.randint( 16, n-total_frames )
          # idx = 17

          # Select the data around the sampled points
          data_sel = data[ the_key ][idx:idx+total_frames ,:]

          data_to_transform = data_sel
          # ugly replicate
          encoder_inputs_ori[i,:,0:self.ori_input_size]  = data_sel[0:self.source_seq_len-1, :]
          decoder_inputs_ori[i,:,0:self.ori_input_size]  = data_sel[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :]
          # get the class info
          if self.one_hot:
              class_encoding = data_sel[0,-len(actions):]
              data_to_transform = data_sel[:,:-len(actions)]
              encoder_inputs[i,:,:,-len(actions):] = class_encoding
              decoder_inputs[i,:,:,-len(actions):] = class_encoding
              encoder_inputs_ori[i,:,-len(actions):] = class_encoding
              decoder_inputs_ori[i,:,-len(actions):] = class_encoding
          # do the transfrom
          encoder_input = np.reshape(
            data_to_transform[0:self.source_seq_len-1,:], [-1, self.NUM_JOINT, 3])
          decoder_input = np.reshape(
            data_to_transform[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :],
            [-1, self.NUM_JOINT, 3]
          )
          decoder_output = data_to_transform[self.source_seq_len:, :]

          # Add the data
          encoder_inputs[i,:,:,0:3]  = encoder_input
          decoder_inputs[i,:,:,0:3]  = decoder_input
          decoder_outputs[i,:,0:self.HUMAN_SIZE] = decoder_output


        # alter data to expected form
        encoder_inputs = torch.tensor(encoder_inputs,dtype=self.dtype).permute(1,0,2,3).to(self.device)
        decoder_inputs = torch.tensor(decoder_inputs,dtype=self.dtype).permute(1,0,2,3).to(self.device)
        decoder_outputs = torch.tensor(decoder_outputs,dtype=self.dtype).permute(1,0,2).to(self.device)

        transform = lambda x: torch.tensor(x, dtype=self.dtype).permute(1,0,2).to(self.device)
        encoder_inputs_ori = transform(encoder_inputs_ori)
        decoder_inputs_ori = transform(decoder_inputs_ori)

        return encoder_inputs, decoder_inputs, decoder_outputs, encoder_inputs_ori, decoder_inputs_ori


    def find_indices_srnn( self, data, action, original_format=False):
        """
        Find the same action indices as in SRNN.
        See https://github.com/asheshjain399/RNNexp/blob/master/structural_rnn/CRFProblems/H3.6m/processdata.py#L325
        """

        # Used a fixed dummy seed, following
        # https://github.com/asheshjain399/RNNexp/blob/srnn/structural_rnn/forecastTrajectories.py#L29
        SEED = 1234567890
        rng = np.random.RandomState( SEED )

        subject = 5
        subaction1 = 1
        subaction2 = 2

        T1 = data[ (subject, action, subaction1, 'even') ].shape[0]
        T2 = data[ (subject, action, subaction2, 'even') ].shape[0]
        prefix, suffix = 50, 100

        idx = []
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        idx.append( rng.randint( 16,T1-prefix-suffix ))
        idx.append( rng.randint( 16,T2-prefix-suffix ))
        return idx

    def get_batch_srnn(self, data, action):
        """
        Get a random batch of data from the specified bucket, prepare for step.

        Args
          data: dictionary with k:v, k=((subject, action, subsequence, 'even')),
            v=nxd matrix with a sequence of poses
          action: the action to load data from
        Returns
          The tuple (encoder_inputs, decoder_inputs, decoder_outputs);
          the constructed batches have the proper format to call step(...) later.
        """

        actions = ["directions", "discussion", "eating", "greeting", "phoning",
                  "posing", "purchases", "sitting", "sittingdown", "smoking",
                  "takingphoto", "waiting", "walking", "walkingdog", "walkingtogether"]

        if not action in actions:
          raise ValueError("Unrecognized action {0}".format(action))

        frames = {}
        frames[ action ] = self.find_indices_srnn( data, action )

        batch_size = 8 # we always evaluate 8 seeds
        subject    = 5 # we always evaluate on subject 5
        source_seq_len = self.source_seq_len
        target_seq_len = self.target_seq_len

        seeds = [( action, (i%2)+1, frames[action][i] ) for i in range(batch_size)]

        encoder_inputs  = np.zeros((batch_size, self.source_seq_len-1, self.NUM_JOINT, self.input_size), dtype=float)
        decoder_inputs  = np.zeros((batch_size, self.target_seq_len, self.NUM_JOINT, self.input_size), dtype=float)
        decoder_outputs = np.zeros((batch_size, self.target_seq_len, self.HUMAN_SIZE), dtype=float)

        # Compute the number of frames needed
        total_frames = source_seq_len + target_seq_len

        # Reproducing SRNN's sequence subsequence selection as done in
        # https://github.com/asheshjain399/RNNexp/blob/master/structural_rnn/CRFProblems/H3.6m/processdata.py#L343
        for i in xrange( batch_size ):

          _, subsequence, idx = seeds[i]
          idx = idx + 50

          data_sel = data[ (subject, action, subsequence, 'even') ]

          data_sel = data_sel[(idx-source_seq_len):(idx+target_seq_len) ,:]

          # add transform to coordinate

          data_to_transform = data_sel
          # get the class info
          if self.one_hot:
              class_encoding = data_sel[0,-len(actions):]
              data_to_transform = data_sel[:,:-len(actions)]
              data_sel = data_sel[:,:-len(actions)]
              encoder_inputs[i,:,:,-len(actions):] = class_encoding
              decoder_inputs[i,:,:,-len(actions):] = class_encoding
          # do the transfrom
          encoder_input = np.reshape(
            data_to_transform[0:self.source_seq_len-1,:], [-1, self.NUM_JOINT, 3])
          decoder_input = np.reshape(
            data_to_transform[self.source_seq_len-1:self.source_seq_len+self.target_seq_len-1, :],
            [-1, self.NUM_JOINT, 3]
          )
          decoder_output = data_to_transform[self.source_seq_len:, :]
          # Add the data
          encoder_inputs[i,:,:,0:3]  = encoder_input
          decoder_inputs[i,:,:,0:3]  = decoder_input
          decoder_outputs[i,:,0:self.HUMAN_SIZE] = decoder_output

        encoder_inputs = torch.tensor(encoder_inputs,dtype=self.dtype).permute(1,0,2,3).to(self.device)
        decoder_inputs = torch.tensor(decoder_inputs,dtype=self.dtype).permute(1,0,2,3).to(self.device)
        decoder_outputs = torch.tensor(decoder_outputs,dtype=self.dtype).permute(1,0,2).to(self.device)

        return encoder_inputs, decoder_inputs, decoder_outputs

class GNNEncoder(nn.Module):
    def __init__(self,
                node_hidden_dim,
                node_out_dim,
                edge_hidden_dim,
                edge_out_dim,
                rec_encode,
                send_encode,
                num_passing=1,
                weight_share=False,
                do_prob=0):

        super(GNNEncoder, self).__init__()
        self.edge_hidden_dim = edge_hidden_dim
        self.edge_out_dim = edge_out_dim
        self.node_hidden_dim = node_hidden_dim
        self.node_out_dim = node_out_dim
        self.do_prob = do_prob
        self.num_passing = num_passing
        self.rec_encode = rec_encode
        self.send_encode = send_encode
        self.weight_share = weight_share
        self.edge_decode = nn.Sequential(
            nn.Linear(self.edge_out_dim, 1),
            nn.Sigmoid()
        )
        print("Dim of hidden node state is ", self.node_hidden_dim)
        print("Dim of hidden edge state is ", self.edge_hidden_dim)

        if weight_share and num_passing > 0:
            self.passing_list = nn.ModuleList([
                MLP(self.node_out_dim * 2, self.edge_hidden_dim, self.edge_out_dim, do_prob=do_prob),
                MLP(self.edge_out_dim + self.node_out_dim, self.node_hidden_dim, self.node_out_dim, do_prob=do_prob)
            ])
        elif num_passing > 0:
            passing_list = [
                [MLP(self.node_out_dim * 2, self.edge_hidden_dim, self.edge_out_dim,   do_prob=do_prob),
                MLP(self.edge_out_dim + self.node_out_dim, self.node_hidden_dim, self.node_out_dim, do_prob=do_prob)]
                for _ in range(num_passing)
            ]
            self.passing_list = nn.ModuleList(list(chain(*passing_list)))

    def forward(self, node_feature, edge_weight):
        if edge_weight is None:
            edge_weight = torch.ones(node_feature.shape[0], self.rec_encode.shape[0], 1).to(node_feature.device)

        node_skip = node_feature
        for passing in range(self.num_passing):
            idx = 0 if self.weight_share else passing // 2
            edge_feature = node2edge(node_feature, self.rec_encode, self.send_encode)
            edge_feature = self.passing_list[idx*2](edge_feature)
            node_feature = edge2node(edge_feature, self.rec_encode, self.send_encode, edge_weight)
            node_feature = torch.cat([node_skip,node_feature], dim = -1)
            node_feature = self.passing_list[idx*2+1](node_feature)
            node_skip = node_feature
        return node_feature, self.edge_decode(edge_feature)

# equivalent to an RNN with graph
class RGNN(nn.Module):
    def __init__(self,
                input_size,
                node_hidden_dim,
                node_out_dim,
                edge_hidden_dim,
                edge_out_dim,
                rec_encode,
                send_encode,
                num_passing=1,
                do_prob=0,
                dtype=torch.float32
                ):
        super(RGNN, self).__init__()
        self.node_hidden_dim = node_hidden_dim
        self.node_out_dim = node_out_dim
        self.edge_hidden_dim = edge_hidden_dim
        self.edge_out_dim = edge_out_dim
        self.dtype=dtype
        self.rec_encode = rec_encode
        self.send_encode = send_encode
        #transfer from node embedding to edge embedding
        self.GNN = GNNEncoder(
            # self.node_out_dim,
            self.node_hidden_dim,
            self.node_out_dim,
            self.edge_hidden_dim,
            self.edge_out_dim,
            self.rec_encode,
            self.send_encode,
            num_passing,
            weight_share=False,
            do_prob=do_prob
        )
        # self.input_size = input_size + self.edge_out_dim
        self.JOINT_DIM = 21
        self.input_size = input_size

        # each joint has a gru if not sharing weight
        self.grus = nn.ModuleList([nn.GRU(input_size=self.input_size,hidden_size=self.node_out_dim) for _ in range(self.JOINT_DIM)])

    # This is the one single step GRU for sharing weight
    # def one_step_forward(self, node_hidden, inputs):
    #     # node_input = inputs.clone()
    #     node_input = self.GNN(inputs.view([1]+node_input.shape))
    #     # Perform GRU for each joint parrallelly
    #     r = torch.sigmoid(self.input_r(node_input) +self.hidden_r(node_hidden))
    #     z = torch.sigmoid(self.input_i(node_input) + self.hidden_i(node_hidden))
    #     n = torch.tanh(self.input_n(node_input) +  r * self.hidden_n(node_hidden))
    #     node_hidden = (1.0 - z) * node_hidden + z * n
    #     # node_hidden = self.GNN(node_hidden)
    #     return node_hidden

    # This is a single step for not sharing weight
    def one_step_forward(self, node_hidden, inputs):
        # inputs = self.GNN(inputs)
        inputs = inputs.unsqueeze(0).permute(2,0,1,3)
        node_hidden = node_hidden.permute(1,0,2).contiguous()

        hidden = []
        for i in range(self.JOINT_DIM):
            hidden.append(self.grus[i](inputs[i],node_hidden[i:i+1])[1])
        hidden = torch.cat(hidden, dim=0).permute(1,0,2).contiguous()
        # hidden = self.GNN(hidden)
        return hidden

    def forward(self, input, hidden):
        outputs = []
        input = input.clone()
        if (len(input.shape) == 3):
            input.unsqueeze_(0)
        if hidden is None:
            hidden = torch.zeros(
                input.shape[1], input.shape[2], self.node_out_dim
            ).to(input.device)
        # do the recurrent nn
        for i, inp in enumerate(input):
            hidden = self.one_step_forward(hidden, inp)
            outputs.append(hidden.unsqueeze(0))
        # the second output is useless, just to compatible to standard rnn
        outputs = torch.cat(outputs,dim=0).to(input.device)
        # return outputs, hidden
        return outputs, hidden

# Two layer MLP module
class MLP(nn.Module):
    def __init__(self, n_in, n_hidden, n_out, do_prob=0):
        super(MLP, self).__init__()
        self.fc1 = nn.Linear(n_in, n_hidden)
        self.fc2 = nn.Linear(n_hidden, n_out)
        self.bn = nn.BatchNorm1d(n_out)
        self.dropout= nn.Dropout(do_prob)

        # self.init_weights()

    def batch_norm(self, inputs):
        orign_shape = inputs.shape
        x = inputs.view(-1, orign_shape[-1])
        x = self.bn(x)
        return x.view(orign_shape)

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_normal_(m.weight.data)
                m.bias.data.fill_(0.1)

    def forward(self, inputs):
        x = F.elu(self.fc1(inputs))
        x = self.dropout(x)
        x = F.elu(self.fc2(x))
        return self.batch_norm(x)
        return x


#do some test here
if __name__ == "__main__":
    # encoder_input = torch.zeros([49, 8, 21,18],requires_grad=False, dtype=torch.float32).cuda()
    encoder_input = torch.zeros([8, 21, 50 * 3 + 15],requires_grad=False, dtype=torch.float32).cuda()
    encoder_input.random_()
    decoder_input = torch.zeros([24, 8, 21, 18], requires_grad=False, dtype=torch.float32).cuda()
    decoder_input.random_()
    #test against a fully connected graph
    off_diag = np.ones([21,21]) - np.eye(21)
    rec_encode = np.array(encode_onehot(np.where(off_diag)[1]),dtype=np.float32)
    send_encode = np.array(encode_onehot(np.where(off_diag)[0]),dtype=np.float32)
    rec_encode = torch.FloatTensor(rec_encode).cuda()
    send_encode = torch.FloatTensor(send_encode).cuda()
    print(rec_encode.shape)

    gnn_model = GNNModel3(
        source_seq_len=50,
        target_seq_len=25,
        edge_hidden_dim=256,
        edge_out_dim=256,
        node_hidden_dim=256,
        node_out_dim=256,
        num_passing=2,
        rec_encode=rec_encode,
        send_encode=send_encode,
        batch_size=8,
        number_of_actions=15,
        device='cuda',
        residual_velocities=True,
        stochastic=True,
        one_hot=True,
        # tied=False,
    )

    gnn_model = gnn_model.to('cuda')

    means, logstds, samples, states = gnn_model(encoder_input, decoder_input)
    target = torch.zeros(means.shape).cuda()
    loss = nn.MSELoss(reduction='mean')(means, target)
    loss.backward()
    print(means.shape)
