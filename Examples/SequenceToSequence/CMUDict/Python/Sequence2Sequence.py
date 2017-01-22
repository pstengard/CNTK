﻿# Copyright (c) Microsoft. All rights reserved.

# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================

from __future__ import print_function
import numpy as np
import os
from cntk import Trainer, Axis
from cntk.io import MinibatchSource, CTFDeserializer, StreamDef, StreamDefs, INFINITELY_REPEAT, FULL_DATA_SWEEP
from cntk.learner import momentum_sgd, momentum_as_time_constant_schedule, learning_rate_schedule, UnitType
from cntk.ops import input_variable, cross_entropy_with_softmax, classification_error, sequence, past_value, future_value, \
                     element_select, alias, hardmax, placeholder_variable, combine, parameter, times
from cntk.ops.functions import CloneMethod, load_model, Function
from cntk.initializer import glorot_uniform
from cntk.utils import log_number_of_parameters, ProgressPrinter, debughelpers
from cntk.graph import find_by_name
from cntk.layers import *
from cntk.models.attention import *

########################
# variables and stuff  #
########################

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Data")
MODEL_DIR = "."
TRAINING_DATA = "cmudict-0.7b.train-dev-20-21.ctf"
TESTING_DATA = "cmudict-0.7b.test.ctf"
VALIDATION_DATA = "tiny.ctf"
VOCAB_FILE = "cmudict-0.7b.mapping"

# model dimensions
input_vocab_dim  = 69
label_vocab_dim  = 69
hidden_dim = 128
num_layers = 2
attention_dim = 128
attention_span = 20
attention_axis = -3
use_attention = True
use_embedding = True
embedding_dim = 200
vocab = ([w.strip() for w in open(os.path.join(DATA_DIR, VOCAB_FILE)).readlines()])
length_increase = 1.5

# sentence-start symbol as a constant
sentence_start = Constant(np.array([w=='<s>' for w in vocab], dtype=np.float32))
sentence_end_index = vocab.index('</s>')
# TODO: move these where they belong

########################
# define the reader    #
########################

def create_reader(path, is_training):
    return MinibatchSource(CTFDeserializer(path, StreamDefs(
        features = StreamDef(field='S0', shape=input_vocab_dim, is_sparse=True),
        labels   = StreamDef(field='S1', shape=label_vocab_dim, is_sparse=True)
    )), randomize = is_training, epoch_size = INFINITELY_REPEAT if is_training else FULL_DATA_SWEEP)

########################
# define the model     #
########################

inputAxis=Axis('inputAxis')
labelAxis=Axis('labelAxis')

def testit(r, with_labels=True):
    #from cntk.blocks import Constant, Type
    if True:
    #try:
        r.dump()
        if with_labels:
            r.update_signature(Type(3, dynamic_axes=[Axis.default_batch_axis(), inputAxis]), 
                               Type(3, dynamic_axes=[Axis.default_batch_axis(), labelAxis]))
        else:
            r.update_signature(Type(3, dynamic_axes=[Axis.default_batch_axis(), inputAxis]))
        r.dump()
        if with_labels:
            res = r.eval({r.arguments[0]: [[[0.9, 0.7, 0.8]]], r.arguments[1]: [[[0, 1, 0]]]})
        else:
            res = r.eval({r.arguments[0]: [[[0.9, 0.7, 0.8]]]})
        print(res)
    #except Exception as e:
        print(e)
        r.dump()     # maybe some updates were already made?
        pass
    #input("hit enter")
    exit()

# create the s2s model
def create_model(): # :: (history*, input*) -> logP(w)*
    # Embedding: (input*) --> (embedded_input*)
    # Right now assumes shared embedding and shared vocab size.
    embed = Embedding(embedding_dim) if use_embedding else identity

    # Encoder: (input*) --> (h0, c0)
    # Create multiple layers of LSTMs by passing the output of the i-th layer
    # to the (i+1)th layer as its input
    # This is the plain s2s encoder. The attention encoder will keep the entire sequence instead.
    # Note: We go_backwards for the plain model, but forward for the attention model.
    with default_options(enable_self_stabilization=True, go_backwards=not use_attention):
        LastRecurrence = Fold if not use_attention else Recurrence
        encode = Sequential([
            embed,
            Stabilizer(),
            For(range(num_layers-1), lambda:
                Recurrence(LSTM(hidden_dim))),
            LastRecurrence(LSTM(hidden_dim), return_full_state=True)
        ])

    # Decoder: (history*, input*) --> z*
    # where history is one of these, delayed by 1 step and <s> prepended:
    #  - training: labels
    #  - testing:  its own output hardmax(z)
    with default_options(enable_self_stabilization=True):
        # sub-layers
        stab_in = Stabilizer()
        rec_blocks = [LSTM(hidden_dim) for i in range(num_layers)]
        stab_out = Stabilizer()
        proj_out = Dense(label_vocab_dim)
        # attention model
        if use_attention:
            attention_model = AttentionModel(attention_dim, attention_span, attention_axis, name='attention_model') # :: (h_enc*, h_dec) -> (h_aug_dec)
        # layer function
        # TODO: refactor such that it takes encoded_input (to be produced by model_train and model_greedy)
        @Function
        def decode(history, x_last):
            input = x_last
            history_axis = history  # we use history_axis wherever we pass this only for the sake of passing its axis
            encoded_input = encode(input)
            r = history
            r = embed(r)
            r = stab_in(r)
            for i in range(num_layers):
                rec_block = rec_blocks[i]   # LSTM(hidden_dim)  # :: (x, dh, dc) -> (h, c)
                if use_attention:
                    if i == 0:
                        @Function
                        # TODO: undo x_last hack
                        def lstm_with_attention(dh, dc, x_last):
                            h_att = attention_model(encoded_input.outputs[0], dh)
                            x_last = splice(x_last, h_att)
                            r = rec_block(dh, dc, x_last)
                            (h, c) = r.outputs                   # BUGBUG: we need 'r', otherwise this will crash with an A/V
                            return (combine([h]), combine([c]))  # BUGBUG: we need combine(), otherwise this will crash with an A/V
                        r = Recurrence(lstm_with_attention)(r)
                    else:
                        r = Recurrence(rec_block)(r)
                else:
                    r = RecurrenceFrom(rec_block)(r, *encoded_input.outputs) # :: r, h0, c0 -> h
            r = stab_out(r)
            r = proj_out(r)
            return r

    return decode

########################
# train action         #
########################

def train(train_reader, valid_reader, vocab, i2w, s2smodel, max_epochs, epoch_size):

    #from cntk.blocks import Constant, Type

    # this is what we train here
    #s2smodel.update_signature(Type(label_vocab_dim, dynamic_axes=[Axis.default_batch_axis(), labelAxis]),
    #                          Type(input_vocab_dim, dynamic_axes=[Axis.default_batch_axis(), inputAxis]))
    # BUGBUG: fails with "Currently if an operand of a elementwise operation has any dynamic axes, those must match the dynamic axes of the other operands"
    #         Maybe also attributable to a parameter-order mix-up?
    # Need to think whether this makes sense. The axes are different for training and testing.

    # model used in training (history is known from labels)
    # note: the labels must not contain the initial <s>
    @Function
    def model_train(input, x_last): # (input*, labels*) --> (word_logp*)
        labels = x_last

        # The input to the decoder always starts with the special label sequence start token.
        # Then, use the previous value of the label sequence (for training) or the output (for execution).
        # BUGBUG: This will fail with sparse input.
        past_labels = Delay(initial_state=sentence_start)(labels)
        return s2smodel(past_labels, input)

    # model used in (greedy) decoding (history is decoder's own output)
    @Function
    def model_greedy(input): # (input*) --> (word_sequence*)

        # Decoding is an unfold() operation starting from sentence_start.
        # We must transform s2smodel (history*, input* -> word_logp*) into a generator (history* -> output*)
        # which holds 'input' in its closure.
        unfold = UnfoldFrom(s2smodel(..., input) >> hardmax,
                            until_predicate=lambda w: w[...,sentence_end_index],  # stop once sentence_end_index was max-scoring output
                            length_increase=length_increase, initial_state=sentence_start)
        return unfold(dynamic_axes_like=input)

    model_greedy.update_signature(Type(input_vocab_dim, dynamic_axes=[Axis.default_batch_axis(), inputAxis]))
    #model_greedy.dump()

    @Function
    def criterion(input, labels):
        # criterion function must drop the <s> from the labels
        postprocessed_labels = sequence.slice(labels, 1, 0) # <s> A B C </s> --> A B C </s>
        z = model_train(input, postprocessed_labels)
        ce   = cross_entropy_with_softmax(z, postprocessed_labels)
        errs = classification_error      (z, postprocessed_labels)
        return (Function.NamedOutput(loss=ce), Function.NamedOutput(metric=errs))
    try:
      #criterion.dump()
      criterion.update_signature(input=Type(input_vocab_dim, dynamic_axes=[Axis.default_batch_axis(), inputAxis]), 
                               labels=Type(label_vocab_dim, dynamic_axes=[Axis.default_batch_axis(), labelAxis]))
    except:
      #criterion.dump()
      raise
    debughelpers.dump_signature(criterion)
    #debughelpers.dump_function(criterion)

    # for this model during training we wire in a greedy decoder so that we can properly sample the validation data
    # This does not need to be done in training generally though
    # Instantiate the trainer object to drive the model training
    lr_per_sample = learning_rate_schedule(0.005, UnitType.sample)
    minibatch_size = 72
    momentum_time_constant = momentum_as_time_constant_schedule(1100)
    clipping_threshold_per_sample = 2.3
    gradient_clipping_with_truncation = True
    learner = momentum_sgd(model_train.parameters,
                           lr_per_sample, momentum_time_constant,
                           gradient_clipping_threshold_per_sample=clipping_threshold_per_sample, 
                           gradient_clipping_with_truncation=gradient_clipping_with_truncation)
    trainer = Trainer(None, criterion, learner)

    # Get minibatches of sequences to train with and perform model training
    i = 0
    mbs = 0
    sample_freq = 100

    # print out some useful training information
    log_number_of_parameters(model_train) ; print()
    progress_printer = ProgressPrinter(freq=30, tag='Training')

    # dummy for printing the input sequence below
    #from cntk import Function
    I = Constant(np.eye(input_vocab_dim))
    @Function
    def noop(input):
        return times(input, I)
    noop.update_signature(Type(input_vocab_dim, is_sparse=True))

    for epoch in range(max_epochs):

        while i < (epoch+1) * epoch_size:
            # get next minibatch of training data
            mb_train = train_reader.next_minibatch(minibatch_size)
            #trainer.train_minibatch({find_arg_by_name('raw_input' , model_train) : mb_train[train_reader.streams.features], 
            #                         find_arg_by_name('raw_labels', model_train) : mb_train[train_reader.streams.labels]})
            trainer.train_minibatch(mb_train[train_reader.streams.features], mb_train[train_reader.streams.labels])

            progress_printer.update_with_trainer(trainer, with_metric=True) # log progress

            # every N MBs evaluate on a test sequence to visually show how we're doing
            if mbs % sample_freq == 0:
                mb_valid = valid_reader.next_minibatch(minibatch_size)
                
                q = noop(mb_valid[valid_reader.streams.features])
                print_sequences(q, i2w)
                print(end=" -> ")

                # run an eval on the decoder output model (i.e. don't use the groundtruth)
                #e = model_greedy(mb_valid[valid_reader.streams.features])
                #print_sequences(e, i2w)

                # debugging attention (uncomment to print out current attention window on validation sequence)
                #if use_attention:
                #    debug_attention(model_greedy, mb_valid[valid_reader.streams.features])

            i += mb_train[train_reader.streams.labels].num_samples
            mbs += 1

        # log a summary of the stats for the epoch
        progress_printer.epoch_summary(with_metric=True)
        
        # save the model every epoch
        model_filename = os.path.join(MODEL_DIR, "model_epoch%d.cmf" % epoch)
        
        # NOTE: we are saving the model with the greedy decoder wired-in. This is NOT necessary and in some
        # cases it would be better to save the model without the decoder to make it easier to wire-in a 
        # different decoder such as a beam search decoder. For now we save this one though so it's easy to 
        # load up and start using.
        model_greedy.save_model(model_filename)
        print("Saved model to '%s'" % model_filename)

########################
# write action         #
########################

def write(reader, model, vocab, i2w):
    
    minibatch_size = 1024
    progress_printer = ProgressPrinter(tag='Evaluation')
    
    while True:
        # get next minibatch of data
        mb = reader.next_minibatch(minibatch_size)
        if not mb:
            break

        # TODO: just use __call__() syntax
        e = model.eval({find_arg_by_name('raw_input' , model) : mb[reader.streams.features], 
                        find_arg_by_name('raw_labels', model) : mb[reader.streams.labels]})
        print_sequences(e, i2w)
        
        progress_printer.update(0, mb[reader.streams.labels].num_samples, None)

#######################
# test action         #
#######################

def test(reader, model, num_minibatches=None):
    
    # we use the test_minibatch() function so need to setup a trainer
    label_sequence = sequence.slice(find_arg_by_name('raw_labels', model), 1, 0)
    lr = learning_rate_schedule(0.007, UnitType.sample)
    momentum = momentum_as_time_constant_schedule(1100) # BUGBUG: use Evaluator

    # BUGBUG: Must do the same as in train(), drop the first token
    ce = cross_entropy_with_softmax(model, label_sequence)
    errs = classification_error(model, label_sequence)
    trainer = Trainer(model, ce, errs, [momentum_sgd(model.parameters, lr, momentum)])

    test_minibatch_size = 1024

    # Get minibatches of sequences to test and perform testing
    i = 0
    total_error = 0.0
    while True:
        mb = reader.next_minibatch(test_minibatch_size)
        if not mb: break
        mb_error = trainer.test_minibatch({find_arg_by_name('raw_input' , model) : mb[reader.streams.features], 
                                           find_arg_by_name('raw_labels', model) : mb[reader.streams.labels]})
        total_error += mb_error
        i += 1
        
        if num_minibatches != None:
            if i == num_minibatches:
                break

    # and return the test error
    return total_error/i

########################
# interactive session  #
########################

def translate_string(input_string, model, vocab, i2w, show_attention=False, max_label_length=20):

    vdict = {vocab[i]:i for i in range(len(vocab))}
    w = [vdict["<s>"]] + [vdict[w] for w in input_string] + [vdict["</s>"]]
    
    features = np.zeros([len(w),len(vdict)], np.float32)
    for t in range(len(w)):
        features[t,w[t]] = 1    
    
    l = [vdict["<s>"]] + [0 for i in range(max_label_length)]
    labels = np.zeros([len(l),len(vdict)], np.float32)
    for t in range(len(l)):
        labels[t,l[t]] = 1
    
    #pred = model.eval({find_arg_by_name('raw_input' , model) : [features], 
    #                   find_arg_by_name('raw_labels', model) : [labels]})
    pred = model([features], [labels])
    
    # print out translation and stop at the sequence-end tag
    print(input_string, "->", end='')
    tlen = 1 # length of the output sequence
    prediction = np.argmax(pred, axis=2)[0]
    for i in prediction:
        phoneme = i2w[i]
        if phoneme == "</s>": break
        tlen += 1
        print(phoneme, end=' ')
    print()
    
    # show attention window (requires matplotlib, seaborn, and pandas)
    if show_attention:
    
        import matplotlib.pyplot as plt
        import seaborn as sns
        import pandas as pd
    
        att = find_by_name(model, 'attention_weights')
        q = combine([model, att])
        output = q.forward({find_arg_by_name('raw_input' , model) : [features], 
                         find_arg_by_name('raw_labels', model) : [labels]},
                         att.outputs)
                         
        # set up the actual words/letters for the heatmap axis labels
        columns = [i2w[ww] for ww in prediction[:tlen]]
        index = [i2w[ww] for ww in w]
 
        att_key = list(output[1].keys())[0]
        att_value = output[1][att_key]
        
        # get the attention data up to the length of the output (subset of the full window)
        X = att_value[0,:tlen,:len(w)]
        dframe = pd.DataFrame(data=np.fliplr(X.T), columns=columns, index=index)
    
        # show the attention weight heatmap
        sns.heatmap(dframe)
        plt.show()

def interactive_session(model, vocab, i2w, show_attention=False):

    import sys

    while True:
        user_input = input("> ").upper()
        if user_input == "QUIT":
            break
        translate_string(user_input, model, vocab, i2w, show_attention=True)
        sys.stdout.flush()

########################
# helper functions     #
########################

def get_vocab(path):
    # get the vocab for printing output sequences in plaintext
    vocab = [w.strip() for w in open(path).readlines()]
    i2w = { i:w for i,w in enumerate(vocab) }
    w2i = { w:i for i,w in enumerate(vocab) }
    
    return (vocab, i2w, w2i)

# Given a vocab and tensor, print the output
def print_sequences(sequences, i2w):
    #for s in sequences:
    #    print([[np.max(w)] for w in s], sep=" ")
    for s in sequences:
        print([i2w[np.argmax(w)] for w in s], sep=" ")

# helper function to find variables by name
# which is necessary when cloning or loading the model
def find_arg_by_name(name, expression):
    vars = [i for i in expression.arguments if i.name == name]
    assert len(vars) == 1
    return vars[0]

# to help debug the attention window
def debug_attention(model, input):
    #q = combine([model, model.attention_model.attention_weights, model.attention_model.u_masked, model.attention_model.h_enc_valid])
    #words, p, u, v = q(input)
    q = combine([model, model.attention_model.attention_weights])
    words, p = q(input)
    len = words.shape[attention_axis-1]
    span = 7 #attention_span  #7 # test sentence is 7 tokens long
    p_sq = np.squeeze(p[0,:len,:span,0,:]) # (batch, len, attention_span, 1, vector_dim)
    #u_sq = np.squeeze(u[0,:len,:span,0,:]) # (batch, len, attention_span, 1, vector_dim)
    #v_sq = np.squeeze(v[0,:len,:span,0,:]) # (batch, len, attention_span, 1, vector_dim)
    #print(p_sq.shape, p_sq, u_sq, v_sq)
    print(p_sq.shape, p_sq)

#############################
# main function boilerplate #
#############################

if __name__ == '__main__':

    from _cntk_py import set_computation_network_trace_level, set_fixed_random_seed, force_deterministic_algorithms
    set_fixed_random_seed(1)  # BUGBUG: has no effect at present  # TODO: remove debugging facilities once this all works

    # test for multi-input plus()
    #from cntk.ops import plus, element_times, max, min, log_add_exp
    #for op in (log_add_exp, max, min, plus, element_times):
    #    s4 = op(Placeholder(name='a'), Placeholder(3, name='b'), Placeholder(4, name='c'), Placeholder(5, name='d'), name='s4')
    #    s4.dump('s4')
    #sequence_reduce_max = Fold(max)
    #sequence_reduce_max.dump('sequence_reduce_max')
    # TODO: create proper test case for this


    #L = Dense(500)
    #L1 = L.clone(CloneMethod.clone)
    #x = placeholder_variable()
    #y = L(x) + L1(x)

    #L = Dense(500)
    #o = L.outputs
    #sh = L.shape
    #W = L.W
    #w = L.weights

    # repro for as_block
    from cntk import placeholder_variable, combine, alias, as_block
    def f(x,y):
        return y-x
    arg_names = ['x', 'y']
    args = [placeholder_variable(name=name) for name in arg_names]
    block_args = [placeholder_variable(name=arg.name) for arg in args]  # placeholders inside the BlockFunction
    combined_block_args = combine(block_args)                           # the content of the BlockFunction
    arg_map = list(zip(block_args, args))                               # after wrapping, the block_args map to args
    combined_args = as_block(composite=combined_block_args, block_arguments_map=arg_map, block_op_name='f_parameter_pack')
    funargs = combined_args.outputs       # the Python function is called with these instead
    #combined_args=None
    out = f(*funargs)
    out_arg_names = [arg.name for arg in out.arguments]
    #out = Recurrence(out, initial_state=13.0)
    #out_arg_names = [arg.name for arg in out.arguments]
    out = out.clone(CloneMethod.share, {out.arguments[0]: input_variable(1, name='x1'), out.arguments[1]: input_variable(1, name='y1')})
    out_arg_names = [arg.name for arg in out.arguments]
    res = out.eval({out.arguments[0]: [[3.0]], out.arguments[1]: [[5.0]]})
    #res = out.eval([[3.0]])

    # repro for name loss
    #from cntk import plus, as_block
    #from _cntk_py import InferredDimension
    #arg = placeholder_variable()
    #x = times(arg, parameter((InferredDimension,3), init=glorot_uniform()), name='x')
    #x = sequence.first(x)
    #sqr = x*x
    #x1 = sqr.find_by_name('x')
    #sqr2 = as_block(sqr, [(sqr.placeholders[0], placeholder_variable())], 'sqr')
    #sqr2 = combine([sqr2])
    #x2 = sqr2.find_by_name('x')

    #stest = sqr2
    ##stest.dump()
    #stest = stest.replace_placeholders({stest.arguments[0]: input_variable(13)})
    ##stest.dump()

    # hook up data
    train_reader = create_reader(os.path.join(DATA_DIR, TRAINING_DATA), True)
    valid_reader = create_reader(os.path.join(DATA_DIR, VALIDATION_DATA), True)
    vocab, i2w, w2i = get_vocab(os.path.join(DATA_DIR, VOCAB_FILE))

    # create inputs and create model
    #inputs = create_inputs()
    model = create_model()

    # train
    #try:
    train(train_reader, valid_reader, vocab, i2w, model, max_epochs=10, epoch_size=908241)
    #except:
    #    x = input("hit enter")

    # write
    #model = load_model("model_epoch0.cmf")
    #write(valid_reader, model, vocab, i2w)
    
    # test
    #model = load_model("model_epoch0.cmf")
    #test_reader = create_reader(os.path.join(DATA_DIR, TESTING_DATA), False)
    #test(test_reader, model)

    # test the model out in an interactive session
    #print('loading model...')
    #model_filename = "model_epoch0.cmf"
    #model = load_model(model_filename)
    #interactive_session(model, vocab, i2w, show_attention=True)
