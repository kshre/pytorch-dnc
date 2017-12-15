#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import torch.nn as nn
import torch as T
from torch.autograd import Variable as var
import torch.nn.functional as F
import numpy as np
import math

from .flann_index import FLANNIndex
from .util import *
import time


class SparseMemory(nn.Module):

  def __init__(
      self,
      input_size,
      mem_size=512,
      cell_size=32,
      independent_linears=True,
      read_heads=4,
      sparse_reads=4,
      temporal_reads=4,
      num_lists=None,
      index_checks=32,
      gpu_id=-1,
      mem_gpu_id=-1
  ):
    super(SparseMemory, self).__init__()

    self.mem_size = mem_size
    self.cell_size = cell_size
    self.gpu_id = gpu_id
    self.mem_gpu_id = mem_gpu_id
    self.input_size = input_size
    self.independent_linears = independent_linears
    self.K = sparse_reads if self.mem_size > sparse_reads else self.mem_size
    self.KL = temporal_reads if self.mem_size > temporal_reads else self.mem_size
    self.read_heads = read_heads
    self.num_lists = num_lists if num_lists is not None else int(self.mem_size / 100)
    self.index_checks = index_checks

    m = self.mem_size
    w = self.cell_size
    r = self.read_heads
    # The visible memory size: (K * R read heads, forward and backward temporal reads of size KL and least used memory cell)
    self.c = (r * self.K) + (self.KL * 2) + 1

    if self.independent_linears:
      self.read_query_transform = nn.Linear(self.input_size, w*r)
      self.write_vector_transform = nn.Linear(self.input_size, w)
      self.interpolation_gate_transform = nn.Linear(self.input_size, self.c)
      self.write_gate_transform = nn.Linear(self.input_size, 1)
      T.nn.init.orthogonal(self.read_query_transform.weight)
      T.nn.init.orthogonal(self.write_vector_transform.weight)
      T.nn.init.orthogonal(self.interpolation_gate_transform.weight)
      T.nn.init.orthogonal(self.write_gate_transform.weight)
    else:
      self.interface_size = (r * w) + w + self.c + 1
      self.interface_weights = nn.Linear(self.input_size, self.interface_size)
      T.nn.init.orthogonal(self.interface_weights.weight)

    self.I = cuda(1 - T.eye(self.c).unsqueeze(0), gpu_id=self.gpu_id)  # (1 * n * n)
    self.δ = 0.005  # minimum usage
    self.timestep = 0

  def rebuild_indexes(self, hidden, erase=False):
    b = hidden['memory'].size(0)

    # if indexes already exist, we reset them
    if 'indexes' in hidden:
      [x.reset() for x in hidden['indexes']]
    else:
      # create new indexes
      hidden['indexes'] = \
          [FLANNIndex(cell_size=self.cell_size,
                 nr_cells=self.mem_size, K=self.K, num_kdtrees=self.num_lists,
                 probes=self.index_checks, gpu_id=self.mem_gpu_id) for x in range(b)]

    # add existing memory into indexes
    pos = hidden['read_positions'].squeeze().data.cpu().numpy()
    if not erase:
      for n, i in enumerate(hidden['indexes']):
        i.reset()
        i.add(hidden['memory'][n], last=pos[n][-1])
    else:
      self.timestep = 0

    return hidden

  def reset(self, batch_size=1, hidden=None, erase=True):
    m = self.mem_size
    w = self.cell_size
    b = batch_size
    r = self.read_heads
    c = self.c

    if hidden is None:
      hidden = {
          # warning can be a huge chunk of contiguous memory
          'memory': cuda(T.zeros(b, m, w).fill_(δ), gpu_id=self.mem_gpu_id),
          'visible_memory': cuda(T.zeros(b, c, w).fill_(δ), gpu_id=self.mem_gpu_id),
          'link_matrix': cuda(T.zeros(b, m, self.KL*2), gpu_id=self.gpu_id),
          'rev_link_matrix': cuda(T.zeros(b, m, self.KL*2), gpu_id=self.gpu_id),
          'precedence': cuda(T.zeros(b, m), gpu_id=self.gpu_id),
          'read_weights': cuda(T.zeros(b, m).fill_(δ), gpu_id=self.gpu_id),
          'write_weights': cuda(T.zeros(b, m).fill_(δ), gpu_id=self.gpu_id),
          'read_vectors': cuda(T.zeros(b, r, w).fill_(δ), gpu_id=self.gpu_id),
          'least_used_mem': cuda(T.zeros(b, 1).fill_(c+1), gpu_id=self.gpu_id).long(),
          'usage': cuda(T.zeros(b, m).fill_(δ), gpu_id=self.gpu_id),
          'read_positions': cuda(T.arange(0, c).expand(b, c), gpu_id=self.gpu_id).long()
      }
      hidden = self.rebuild_indexes(hidden, erase=True)
    else:
      hidden['memory'] = hidden['memory'].clone()
      hidden['visible_memory'] = hidden['visible_memory'].clone()
      hidden['link_matrix'] = hidden['link_matrix'].clone()
      hidden['rev_link_matrix'] = hidden['link_matrix'].clone()
      hidden['precedence'] = hidden['precedence'].clone()
      hidden['read_weights'] = hidden['read_weights'].clone()
      hidden['write_weights'] = hidden['write_weights'].clone()
      hidden['read_vectors'] = hidden['read_vectors'].clone()
      hidden['least_used_mem'] = hidden['least_used_mem'].clone()
      hidden['usage'] = hidden['usage'].clone()
      hidden['read_positions'] = hidden['read_positions'].clone()
      hidden = self.rebuild_indexes(hidden, erase)

      if erase:
        hidden['memory'].data.fill_(δ)
        hidden['visible_memory'].data.fill_(δ)
        hidden['link_matrix'].data.zero_()
        hidden['rev_link_matrix'].data.zero_()
        hidden['precedence'].data.zero_()
        hidden['read_weights'].data.fill_(δ)
        hidden['write_weights'].data.fill_(δ)
        hidden['read_vectors'].data.fill_(δ)
        hidden['least_used_mem'].data.fill_(c+1+self.timestep)
        hidden['usage'].data.fill_(δ)
        hidden['read_positions'] = cuda(T.arange(self.timestep, c+self.timestep).expand(b, c), gpu_id=self.gpu_id).long()

    return hidden

  def write_into_sparse_memory(self, hidden):
    visible_memory = hidden['visible_memory']
    positions = hidden['read_positions'].squeeze()

    (b, m, w) = hidden['memory'].size()
    # update memory
    hidden['memory'].scatter_(1, positions.unsqueeze(2).expand(b, self.c, w), visible_memory)

    # non-differentiable operations
    pos = positions.data.cpu().numpy()
    for batch in range(b):
      # update indexes
      hidden['indexes'][batch].reset()
      hidden['indexes'][batch].add(hidden['memory'][batch], last=pos[batch][-1])

    mem_limit_reached = hidden['least_used_mem'][0].data.cpu().numpy()[0] >= self.mem_size-1
    hidden['least_used_mem'] = (hidden['least_used_mem'] * 0 + self.c + 1) if mem_limit_reached else hidden['least_used_mem'] + 1

    return hidden

  def update_link_matrices(self, link_matrix, rev_link_matrix, write_weights, precedence, temporal_read_positions):
    temporal_read_precedence = precedence.gather(1, temporal_read_positions)
    link_matrix = (1 - write_weights).unsqueeze(2) * link_matrix + write_weights.unsqueeze(2) * temporal_read_precedence.unsqueeze(1)

    temporal_write_weights = write_weights.gather(1, temporal_read_positions)
    rev_link_matrix = (1 - temporal_write_weights).unsqueeze(1) * rev_link_matrix + (temporal_write_weights.unsqueeze(1) * precedence.unsqueeze(2))

    return link_matrix, rev_link_matrix

  def update_precedence(self, precedence, write_weights):
    return (1 - T.sum(write_weights, dim=-1, keepdim=True)) * precedence + write_weights

  def write(self, interpolation_gate, write_vector, write_gate, hidden):

    read_weights = hidden['read_weights'].gather(1, hidden['read_positions'])
    write_weights = hidden['write_weights'].gather(1, hidden['read_positions'])

    hidden['usage'], I = self.update_usage(
        hidden['read_positions'],
        read_weights,
        write_weights,
        hidden['usage']
    )

    # either we write to previous read locations
    x = interpolation_gate * read_weights
    # or to a new location
    y = (1 - interpolation_gate) * I
    write_weights = write_gate * (x + y)

    # store the write weights
    hidden['write_weights'].scatter_(1, hidden['read_positions'], write_weights)

    # erase matrix
    erase_matrix = I.unsqueeze(2).expand(hidden['visible_memory'].size())

    # write into memory
    hidden['visible_memory'] = hidden['visible_memory'] * (1 - erase_matrix) + T.bmm(write_weights.unsqueeze(2), write_vector)
    hidden = self.write_into_sparse_memory(hidden)

    # update link_matrix and precedence
    (b, c) = write_weights.size()

    # update link matrix
    temporal_read_positions = hidden['read_positions'][:, self.read_heads*self.K+1:]
    hidden['link_matrix'], hidden['rev_link_matrix'] = \
      self.update_link_matrices(
        hidden['link_matrix'],
        hidden['rev_link_matrix'],
        hidden['write_weights'],
        hidden['precedence'],
        temporal_read_positions
      )

    # update precedence vector
    hidden['precedence'] = self.update_precedence(hidden['precedence'], hidden['read_weights'])

    return hidden

  def update_usage(self, read_positions, read_weights, write_weights, usage):
    (b, _) = read_positions.size()
    # usage is timesteps since a non-negligible memory access
    # todo store write weights of all mem and gather from that
    u = (read_weights + write_weights > self.δ).float()

    # usage before write
    relevant_usages = usage.gather(1, read_positions)

    # indicator of words with minimal memory usage
    minusage = T.min(relevant_usages, -1, keepdim=True)[0]
    minusage = minusage.expand(relevant_usages.size())
    I = (relevant_usages == minusage).float()

    # usage after write
    relevant_usages = (self.timestep - relevant_usages) * u + relevant_usages * (1 - u)

    usage.scatter_(1, read_positions, relevant_usages)

    return usage, I

  def directional_weightings(self, link_matrix, rev_link_matrix, temporal_read_weights):
    f = T.bmm(link_matrix, temporal_read_weights.unsqueeze(2)).squeeze()
    b = T.bmm(rev_link_matrix, temporal_read_weights.unsqueeze(2)).squeeze()
    return f, b

  def read_from_sparse_memory(self, memory, indexes, keys, least_used_mem, usage, forward, backward, prev_read_positions):
    b = keys.size(0)
    read_positions = []

    # we search for k cells per read head
    for batch in range(b):
      distances, positions = indexes[batch].search(keys[batch])
      read_positions.append(positions)
    read_positions = T.stack(read_positions, 0)

    # add least used mem to read positions
    # TODO: explore possibility of reading co-locations or ranges and such
    (b, r, k) = read_positions.size()
    read_positions = var(read_positions).squeeze(1).view(b, -1)

    # differentiable ops
    # temporal reads,
    # TODO: this results in duplicate reads when the content based positions and temporal ones are same
    (b, m, w) = memory.size()
    # get the top KL entries
    max_length = int(least_used_mem[0, 0].data.cpu().numpy())
    _, fp = T.topk(forward, self.KL, largest=True)
    _, bp = T.topk(backward, self.KL, largest=True)

    # append forward and backward read positions, might lead to duplicates
    read_positions = T.cat([read_positions, fp, bp], 1)
    read_positions = T.cat([read_positions, least_used_mem], 1)
    read_positions = T.clamp(read_positions, 0, max_length)

    visible_memory = memory.gather(1, read_positions.unsqueeze(2).expand(b, self.c, w))

    read_weights = σ(θ(visible_memory, keys), 2)
    read_vectors = T.bmm(read_weights, visible_memory)
    read_weights = T.prod(read_weights, 1)

    return read_vectors, read_positions, read_weights, visible_memory

  def read(self, read_query, hidden):
    # get forward and backward weights
    temporal_read_positions = hidden['read_positions'][:, self.read_heads*self.K+1:]
    read_weights = hidden['read_weights'].gather(1, temporal_read_positions)
    forward, backward = self.directional_weightings(hidden['link_matrix'], hidden['rev_link_matrix'], read_weights)

    # sparse read
    read_vectors, positions, read_weights, visible_memory = \
        self.read_from_sparse_memory(
          hidden['memory'],
          hidden['indexes'],
          read_query,
          hidden['least_used_mem'],
          hidden['usage'],
          forward, backward,
          hidden['read_positions']
        )

    hidden['read_positions'] = positions
    hidden['read_weights'] = hidden['read_weights'].scatter_(1, positions, read_weights)
    hidden['read_vectors'] = read_vectors
    hidden['visible_memory'] = visible_memory

    return hidden['read_vectors'], hidden

  def forward(self, ξ, hidden):
    t = time.time()

    # ξ = ξ.detach()
    m = self.mem_size
    w = self.cell_size
    r = self.read_heads
    c = self.c
    b = ξ.size()[0]

    if self.independent_linears:
      # r read keys (b * r * w)
      read_query = self.read_query_transform(ξ).view(b, r, w)
      # write key (b * 1 * w)
      write_vector = self.write_vector_transform(ξ).view(b, 1, w)
      # write vector (b * 1 * r)
      interpolation_gate = F.sigmoid(self.interpolation_gate_transform(ξ)).view(b, c)
      # write gate (b * 1)
      write_gate = F.sigmoid(self.write_gate_transform(ξ).view(b, 1))
    else:
      ξ = self.interface_weights(ξ)
      # r read keys (b * r * w)
      read_query = ξ[:, :r*w].contiguous().view(b, r, w)
      # write key (b * 1 * w)
      write_vector = ξ[:, r*w: r*w + w].contiguous().view(b, 1, w)
      # write vector (b * 1 * r)
      interpolation_gate = F.sigmoid(ξ[:, r*w + w: r*w + w + c]).contiguous().view(b, c)
      # write gate (b * 1)
      write_gate = F.sigmoid(ξ[:, -1].contiguous()).unsqueeze(1).view(b, 1)

    self.timestep += 1
    hidden = self.write(interpolation_gate, write_vector, write_gate, hidden)
    return self.read(read_query, hidden)
