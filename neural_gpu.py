# Copyright 2015 Google Inc. All Rights Reserved.
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
# ==============================================================================
"""The Neural GPU Model."""

import time

import tensorflow as tf

import data_utils
import random
import numpy as np
import neural_curriculum

FLAGS = tf.app.flags.FLAGS

def tf_cut_function(val, vlo, vhi, glo, ghi):
  if vlo is None:
    return val
  a = tf.clip_by_value(val, vlo, vhi)
  if glo is None:
    return a
  assert ghi >= vhi > vlo >= glo
  zz = tf.clip_by_value(val, glo, ghi)
  return zz - tf.stop_gradient(zz - a)

def sigmoid_cutoff(x, cutoff):
  """Sigmoid with cutoff, e.g., 1.2sigmoid(x) - 0.1."""
  y = tf.sigmoid(x)
  if cutoff < 1.01: return y
  d = (cutoff - 1.0) / 2.0
  z = cutoff * y - d
  dd = (FLAGS.smooth_grad - 1.0) / 2.0 if FLAGS.smooth_grad else None
  glo, ghi = (-dd, 1+dd) if FLAGS.smooth_grad else (None, None)
  return tf_cut_function(z, 0, 1, glo, ghi)


def tanh_cutoff(x, cutoff):
  """Tanh with cutoff, e.g., 1.1tanh(x) cut to [-1. 1]."""
  y = tf.tanh(x)
  if cutoff < 1.01: return y
  z = cutoff * y
  tcut = FLAGS.smooth_grad_tanh
  glo, ghi = (-tcut, tcut) if tcut else (None, None)
  return tf_cut_function(z, -1, 1, glo, ghi)

def conv_linear(arg, kw, kh, nin, nout, do_bias, bias_start, prefix):
  """Convolutional linear map."""
  with tf.variable_scope(prefix):
    k = tf.get_variable("CvK", [kw, kh, nin, nout])
    res = conv2d(arg, k, [1, 1, 1, 1], "SAME")
    if not do_bias: return res
    bias_term = tf.get_variable("CvB", [nout],
                                initializer=tf.constant_initializer(0.0))
    return res + bias_term + bias_start

def conv_gru(mem, kw, kh, nmaps, cutoff, prefix):
  """Convolutional GRU."""
  def conv_lin(arg, suffix, bias_start):
    return conv_linear(arg, kw, kh, nmaps, nmaps, True, bias_start,
                       prefix + "/" + suffix)
  reset = sigmoid_cutoff(conv_lin(mem, "r", 1.0), cutoff)
  candidate = tanh_cutoff(conv_lin(reset * mem, "c", 0.0), FLAGS.cutoff_tanh)
  gate = sigmoid_cutoff(conv_lin(mem, "g", 1.0), cutoff)
  return gate * mem + (1 - gate) * candidate

def gru_block(nconvs, cur, kw, kh, nmaps, cutoff, mask, suffix):
  # Do nconvs-many CGRU steps.
  for layer in xrange(nconvs):
    cur = conv_gru(cur, kw, kh, nmaps, cutoff, "cgru_%d_%s" % (layer, suffix))
    cur *= mask
  return cur

try:
  @tf.RegisterGradient("CustomIdG")
  def _custom_id_grad(_, grads):
    return grads
except KeyError as e: # Happens on reload
  pass

def quantize(t, quant_scale, max_value=1.0):
  """Quantize a tensor t with each element in [-max_value, max_value]."""
  t = tf.minimum(max_value, tf.maximum(t, -max_value))
  big = quant_scale * (t + max_value) + 0.5
  with tf.get_default_graph().gradient_override_map({"Floor": "CustomIdG"}):
    res = (tf.floor(big) / quant_scale) - max_value
  return res


def quantize_weights_op(quant_scale, max_value):
  ops = [v.assign(quantize(v, quant_scale, float(max_value)))
         for v in tf.trainable_variables()]
  return tf.group(*ops)


def relaxed_average(var_name_suffix, rx_step):
  """Calculate the average of relaxed variables having var_name_suffix."""
  relaxed_vars = []
  for l in xrange(rx_step):
    with tf.variable_scope("RX%d" % l, reuse=True):
      try:
        relaxed_vars.append(tf.get_variable(var_name_suffix))
      except ValueError:
        pass
  dsum = tf.add_n(relaxed_vars)
  avg = dsum / len(relaxed_vars)
  diff = [v - avg for v in relaxed_vars]
  davg = tf.add_n([d*d for d in diff])
  return avg, tf.reduce_sum(davg)


def relaxed_distance(rx_step):
  """Distance between relaxed variables and their average."""
  res, ops, rx_done = [], [], {}
  for v in tf.trainable_variables():
    vals = v.op.name.split('/', 2)
    if vals[1].startswith('RX'):
      rx_name = vals[2]
      if rx_name not in rx_done:
        avg, dist_loss = relaxed_average(rx_name, rx_step)
        res.append(dist_loss)
        rx_done[rx_name] = avg
      ops.append(v.assign(rx_done[rx_name]))
  return tf.add_n(res), tf.group(*ops)


def make_dense(targets, noclass):
  """Move a batch of targets to a dense 1-hot representation."""
  if True:#with tf.device("/cpu:0"):
    shape = tf.shape(targets)
    batch_size = shape[0]
    indices = targets + noclass * tf.range(0, batch_size)
    length = tf.expand_dims(batch_size * noclass, 0)
    dense = tf.sparse_to_dense(indices, length, 1.0, 0.0)
  return tf.reshape(dense, [-1, noclass])

def tf_shape(tensor):
  """Return the tensor shape in a form tf.reshape understands."""
  return [x or -1 for x in tensor.get_shape().as_list()]

from functools import wraps

def fix_batching(f, k, nargs=1):
  """Make a given function f support extra initial dimensions.

  A number of tf.nn operations expect shapes of the form [-1] + lst
  where len(lst) is a fixed constant, and operate independently on the
  -1.  This lets them work on shapes of the form lst2 + lst, where
  lst2 is arbitrary.

  args:
    k: len(lst) that f wants
    nargs: Number of tensors with this property
  """
  @wraps(f)
  def wrapper(*args, **kws):
    arrays = args[:nargs]
    old_shape = tf_shape(arrays[0])
    used_shape = old_shape[-k:]
    inputs_reshaped = tuple(tf.reshape(array, [-1]+used_shape)
                       for array in arrays)
    output = f(*(inputs_reshaped + args[nargs:]), **kws)
    new_prefix = old_shape[:-k]
    new_suffix = tf_shape(output)[1:]
    output_reshaped = tf.reshape(output, new_prefix + new_suffix)
    return output_reshaped
  return wrapper

softmax = fix_batching(tf.nn.softmax, 1)
conv2d = fix_batching(tf.nn.conv2d, 3)
softmax_cross_entropy_with_logits = fix_batching(tf.nn.softmax_cross_entropy_with_logits, 1, 2)

def safe_squeeze(array, i):
  shape = tf_shape(array)
  assert shape[i] == 1
  return tf.reshape(array, shape[:i] + (shape[i+1:] if (i+1) else []))

def expand_dims_by_k(array, k):
  return tf.reshape(array, tf_shape(array) + [1]*k)

class NeuralGPUAtSize(object):
  """Instantiate the NeuralGPU at a given block size."""
  def __init__(self, model, length, adam):
    self.config = model.config
    self.length = length
    # batch_size x length
    self.input = tf.concat(1, [tf.expand_dims(i, 1) for i in model.input[:length]])
    self.target = tf.concat(1, [tf.expand_dims(t, 1) for t in model.target[:length]])
    #tf.concat(1, [tf.reshape(i, [-1, 1]) for i in model.target[:length]])
    self.emb_weights = model.emb_weights
    self.e0 = model.e0
    self.do_training = model.do_training

    self.model = model

    self.task = model.task

    self.construct_graph(adam)

  def construct_mask(self) :
    # Mask to 0-out padding space in each step.
    # bmask: batch_size x length
    bmask = (self.input > 0) | (self.target > 0)
    # mask: batch x length x 1 x 1
    mask = tf.to_float(expand_dims_by_k(bmask, 2))

    # padded_mask: batch x (length+1) x 1 x 1
    padded_mask = tf.concat(1, [mask, tf.zeros_like(mask[:,:1,:,:])])
    # scales: initially batch x length x 1 x 1
    scales = padded_mask[:,:self.length,:,:] * (1 - padded_mask[:,1:,:,:])
    # Now length x batch x 1 x 1
    scales = tf.transpose(scales, [1,0,2,3])
    return mask, scales

  def construct_all_layers(self, first, mask):
    cutoff = self.config.cutoff
    kw = self.config.kw
    kh = self.config.kh
    nmaps = self.config.nmaps
    nconvs = self.config.nconvs

    keep_prob = 1.0 - self.do_training * (self.config.dropout * 8.0 / float(self.length))
    cur = first
    layers = []
    attention_probs_list = []
    for it in xrange(self.length):
      with tf.variable_scope("RX%d" % (it % self.config.rx_step)) as vs:
        if it >= self.config.rx_step:
          vs.reuse_variables()
        cur = tf.nn.dropout(cur, keep_prob) * mask

        if FLAGS.num_attention:
          k = FLAGS.num_attention
          blocks = tf.pack([cur]*(2*k+1))
          result = gru_block(nconvs, blocks, kw, kh, nmaps, cutoff, mask, 'grublocks')
          # shape: (2k+1) x bs x length x height x nmaps
          keys = result[:k,:,:,:,:]
          vals = result[k:2*k,:,:,:,:]
          cur_att = result[2*k,:,:,:,:]
          logits = tf.reduce_sum(keys * cur_att, [-1,-2,-3]) # shape: k x bs
          attention_probs = tf.transpose(softmax(tf.transpose(logits))) # shape: k x bs
          attention_probs_list.append(attention_probs)
          cur = tf.reduce_sum(expand_dims_by_k(attention_probs, 3) * vals, 0)
          # bs x length x height x nmaps

          # parts = tf.unpack(result)
          # cur_att = parts[0]
          # attention_vals = []
          # logit_table = [] # shape: nattention x bs x 1
          # for i, (key, val) in enumerate(zip(parts[1::2], parts[2::2])):
          #   if i in [0,1]:
          #     # self.task shape: bs
          #     val = tf.select(tf.equal(self.task, i), val, tf.stop_gradient(val))
          #     key = tf.select(tf.equal(self.task, i), key, tf.stop_gradient(key))
          #   logit = tf.reduce_sum(cur_att * key, [1,2,3]) # shape: bs
          #   logit_table.append(tf.expand_dims(logit, 1)) 
          #   attention_vals.append(tf.expand_dims(val, 0))

          # attention_probs = tf.transpose(tf.nn.softmax(tf.concat(1, logit_table)))
          # attention_probs_list.append(attention_probs)
          # attention_vals = tf.concat(0, attention_vals) # shape: 3 x bs x len x h xnmaps
          # expanded_probs = attention_probs # make it 3 x bs x 1 x 1 x 1
          # for i in range(3):
          #   expanded_probs = tf.expand_dims(expanded_probs, -1)
          # cur = tf.reduce_sum(expanded_probs * attention_vals, [0])
        else:
          cur = gru_block(nconvs, cur, kw, kh, nmaps, cutoff, mask, 'lookup')

        layers.append(cur)

    self.attention_probs = tf.pack(attention_probs_list) # shape: layers x 3 x bs
    self.layers = tf.pack(layers)
    return layers

  def construct_graph(self, adam):
    nmaps = self.config.nmaps
    vec_size = self.config.nmaps
    noclass = self.config.noclass
    height = self.config.height

    # The general tensor shape is
    # batchsize x length x height x nmaps

    # Embed inputs and calculate mask.
    if True:#with tf.device("/cpu:0"):
      with tf.control_dependencies([self.e0]):
        embedded = tf.nn.embedding_lookup(self.emb_weights, self.input)
      mask, scales = self.construct_mask()

    # start: batch_size x length x nmaps
    start = tf.tanh(embedded)

    # First image comes from start by applying one convolution and adding 0s.
    # first: batch_size x length x height x nmaps
    first = conv_linear(tf.expand_dims(start, 2),
                        1, 1, vec_size, nmaps, True, 0.0, "input")
    first = tf.concat(2, [first] + [tf.zeros_like(first)]*(height - 1))

    # Computation steps.
    layers = self.construct_all_layers(first, mask)

    # Final convolution to get logits, list outputs.
    layer_output = conv_linear(self.layers[:,:,:,:1,:], 1, 1, nmaps, noclass, True, 0.0, "output")
    outputs = safe_squeeze(layer_output, -2) # depth x batch x length x noclass
    output = tf.reduce_sum(outputs * scales, 0)
    self.layer_outputs = softmax(outputs)
    self.output = softmax(output) # batch_size x length x noclass

    # Calculate cross-entropy loss and normalize it.
    targets = tf.one_hot(self.target, noclass)
    xent = softmax_cross_entropy_with_logits(output, targets) # shape: batch x length
    perp_loss = tf.reduce_mean(xent * tf.reshape(mask, [-1, self.length]))

    # Final loss: cross-entropy + shared parameter relaxation part.
    relax_dist, self.model.avg_op = relaxed_distance(self.config.rx_step)
    total_loss = perp_loss + relax_dist * self.model.pull
    self.loss = perp_loss

    # Gradients and Adam update operation.
    if self.length == data_utils.bins[0] or (self.config.mode == 0 and
                                        self.length < data_utils.bins[-1] + 1):
      data_utils.print_out("Creating backward for bin of length %d." % self.length)
      params = tf.trainable_variables()
      grads = tf.gradients(total_loss, params)
      grads, norm = tf.clip_by_global_norm(grads, self.config.max_grad_norm)
      self.grad_norm = norm
      update = adam.apply_gradients(zip(grads, params),
                                    global_step=self.model.global_step)
      self.update = update
    #import ipdb; ipdb.set_trace()

    def __repr__(self):
      return '<NeuralGPUAtSize %s>' % (self.length)

class NeuralGPU(object):
  """Neural GPU Model."""

  def __init__(self, config):
    self.t = time.time()
    self.config = config

    # Feeds for parameters and ops to update them.
    self.global_step = tf.Variable(0, trainable=False)
    self.lr = float(config.lr)
    self.quant_op = quantize_weights_op(512, 8)

    self.pull = float(config.pull)
    self.do_training = tf.placeholder(tf.float32, name="do_training")

    # Feeds for inputs, targets, outputs, losses, etc.
    self.instances = []

    self.input = []
    self.target = []
    for l in xrange(data_utils.forward_max + 1):
      self.input.append(tf.placeholder(tf.int32, shape=(None,), name="inp{0}".format(l)))
      self.target.append(tf.placeholder(tf.int32, shape=(None,), name="tgt{0}".format(l)))
    self.task = tf.placeholder(tf.uint8, shape=(None,), name="task")

    with tf.variable_scope("model") as vs:
      self.construct_graph()
      self.saver = tf.train.Saver(tf.all_variables())

  def construct_graph(self):
    vec_size = self.config.nmaps
    # Computation.
    if True:#with tf.device("/cpu:0"):
      self.emb_weights = tf.get_variable(
          "embedding", [self.config.niclass, vec_size],
          initializer=tf.random_uniform_initializer(-1.7, 1.7))
      self.e0 = tf.scatter_update(self.emb_weights,
                             tf.constant(0, dtype=tf.int32, shape=[1]),
                             tf.zeros([1, vec_size]))

    adam = tf.train.AdamOptimizer(self.lr, epsilon=1e-4, use_locking=True)

    # Main graph creation loop, for every bin in data_utils.
    for length in sorted(list(set(data_utils.bins + [data_utils.forward_max]))):
      data_utils.print_out("Creating model for bin of length %d." % length)
      start_time = time.time()
      self.instances.append(NeuralGPUAtSize(self, length, adam))
      tf.get_variable_scope().reuse_variables() # Later rounds reuse variables
      data_utils.print_out("Created model for bin of length %d in"
                           " %.2f s." % (length, time.time() - start_time))

  def get_instance_for_length(self, length):
    for instance in self.instances:
      if instance.length >= length:
        return instance
    raise IndexError('Max instance size %s; %s is too large!' % (instance.length, length))

  def step(self, sess, batch, do_backward, get_steps=False):
    """Run a step of the network."""
    inp, target, taskid = batch
    assert len(inp) == len(target)
    length = len(target)
    feed_in = {}
    feed_in[self.do_training] = 1.0 if do_backward else 0.0
    feed_in[self.task] = taskid
    feed_out = {}
    for l in xrange(length):
      feed_in[self.input[l]] = inp[l]
      feed_in[self.target[l]] = target[l]
    instance = self.get_instance_for_length(length)
    if do_backward:
      feed_out['back_update'] = instance.update
      feed_out['grad_norm'] = instance.grad_norm
    if get_steps:
      feed_out['step'] = instance.layers
    feed_out['loss'] = instance.loss
    feed_out['layer_outputs'] = instance.layer_outputs
    feed_out['output'] = instance.output
    feed_out['attention'] = instance.attention_probs
    res = sess.run(feed_out, feed_in)
    return neural_curriculum.NeuralGPUResult(res, inp, target, taskid)

  def simple_step(self, sess, a):
    """Run a simple operation on one input.

    Reverses the order for you, so you can input in little endian.
    """
    if isinstance(a, basestring):
      a = [data_utils.to_id(c) for c in a]
    else:
      a = list(a)
    l = self.get_instance_for_length(len(a)).length
    pad = l - len(a)
    input = np.array([a[::-1] + [0]*pad]).T
    result = self.step(sess, (input, input, [0]), False)
    relevant_output = result.output.argmax(axis=-1).T[0, :(-pad if pad else None)]
    return ''.join(map(data_utils.to_symbol, relevant_output[::-1]))
