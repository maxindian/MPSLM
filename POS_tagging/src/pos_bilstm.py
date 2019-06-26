import glob
import sys
import tensorflow as tf
import numpy as np
import time
import math
import os
import shutil
import itertools
from datetime import datetime
from random import shuffle
from preprocess import PreprocessData
from enum import Enum
import sklearn.metrics

MAX_LENGTH = 100
BATCH_SIZE = 128
VALIDATION_FREQUENCY = 10
CHECKPOINT_FREQUENCY = 50
NO_OF_EPOCHS = 6
ORTHOGRAPHIC_HIDDEN_STATE_SIZE=10

class OrthographicInsertionPlace(Enum):
    NONE = 0
    INPUT = 1
    OUTPUT = 2


class OrthographicInsertionType(Enum):
    ONE_HOT = 0
    EMBEDDED = 1
    INT_VALUES = 2


# Returns formatted current time as string
def get_time_string():
    return time.strftime('%c') + ' '


## Model class is adatepd from model.py found here
## https://github.com/monikkinom/ner-lstm/
class Model:
    def __init__(self, input_dim, prefix_orthographic_dim, suffix_orthographic_dim, sequence_len, output_dim,
                 orthographic_insertion_place, orthographic_insertion_type, hidden_state_size=50,
                 orthographic_hidden_state_size=ORTHOGRAPHIC_HIDDEN_STATE_SIZE):
        self._input_dim = input_dim
        self._prefix_orthographic_dim = prefix_orthographic_dim
        self._suffix_orthographic_dim = suffix_orthographic_dim
        self._sequence_len = sequence_len
        self._output_dim = output_dim
        self._hidden_state_size = hidden_state_size
        self._orthographic_hidden_state_size = orthographic_hidden_state_size
        self._orthographic_insertion_place = orthographic_insertion_place
        self._orthographic_insertion_type = orthographic_insertion_type
        self._optimizer = tf.train.AdamOptimizer(0.0005)

    # Adapted from https://github.com/monikkinom/ner-lstm/blob/master/model.py __init__ function
    def create_placeholders(self):
        self._input_words = tf.placeholder(tf.int32, [BATCH_SIZE, self._sequence_len])
        self._prefix_features = tf.placeholder(tf.int32, [BATCH_SIZE, self._sequence_len])
        self._suffix_features = tf.placeholder(tf.int32, [BATCH_SIZE, self._sequence_len])
        self._output_tags = tf.placeholder(tf.int32, [BATCH_SIZE, self._sequence_len])

    def set_input_output(self, input_, output):
        self._input_words = input_
        self._output_tags = output

    ## Returns the mask that is 1 for the actual words
    ## and 0 for the padded part
    # Adapted from https://github.com/monikkinom/ner-lstm/blob/master/model.py __init__ function
    def get_mask(self, t):  # (t - batch size * sequence length ?)
        mask = tf.cast(tf.not_equal(t, -1), tf.int32)  # (mask - batch size * sequence length ?)
        lengths = tf.reduce_sum(mask, reduction_indices=1)  # (lengths - batch size * 1 ?)
        return mask, lengths

    def get_oov_mask(self, t):  # (t - batch size * sequence length ?)
        mask = tf.cast(tf.equal(t, self._input_dim - 2),
                       tf.int32)  # (mask: batch size * sequence length ?) # _input_dim - 2 is for 'unk', input_dim - 1 is for padding
        lengths = tf.reduce_sum(mask, reduction_indices=1)  # (lengths: batch size * 1 ?)
        return mask, lengths

    ## Embed the large one hot input vector into a smaller space
    ## to make the lstm learning tractable
    def get_embedding(self, input_):
        embedding = tf.get_variable("embedding",
                                    [self._input_dim, self._hidden_state_size], dtype=tf.float32)



        return tf.nn.embedding_lookup(embedding, tf.cast(input_, tf.int32))

    ## Embed the large one hot input vector into a smaller space
    ## to make the lstm learning tractable
    def get_orthographic_embedding(self, input_, dimension, embedding_var_name):
        embedding = tf.get_variable(embedding_var_name,
                                    [dimension, self._orthographic_hidden_state_size], dtype=tf.float32)
        return tf.nn.embedding_lookup(embedding, tf.cast(input_, tf.int32))

    # Adapted from https://github.com/monikkinom/ner-lstm/blob/master/model.py __init__ function
    def create_graph(self):
        self.create_placeholders()

        ## Since we are padding the input, we need to give
        ## the actual length of every instance in the batch
        ## so that the backward lstm works properly
        self._mask, self._lengths = self.get_mask(self._output_tags)
        self._oov_mask, self._oov_lengths = self.get_oov_mask(self._input_words)
        self._total_length = tf.reduce_sum(self._lengths)
        self._total_oov_length = tf.reduce_sum(self._oov_lengths)

        ## Embedd the very large input vector into a smaller dimension
        ## This is for computational tractability
        with tf.variable_scope("lstm_input"):
            lstm_input = self.get_embedding(self._input_words)
            if self._orthographic_insertion_place == OrthographicInsertionPlace.INPUT:
                if self._orthographic_insertion_type == OrthographicInsertionType.ONE_HOT:
                    prefix_one_hot = tf.one_hot(self._prefix_features, self._prefix_orthographic_dim)
                    suffix_one_hot = tf.one_hot(self._suffix_features, self._suffix_orthographic_dim)
                    self._hidden_state_size += self._prefix_orthographic_dim + self._suffix_orthographic_dim
                    lstm_input = tf.concat([lstm_input, prefix_one_hot, suffix_one_hot], axis=2)
                elif self._orthographic_insertion_type == OrthographicInsertionType.INT_VALUES:
                    prefix_reshaped = tf.reshape(self._prefix_features, [BATCH_SIZE, self._sequence_len, 1])
                    suffix_reshaped = tf.reshape(self._suffix_features, [BATCH_SIZE, self._sequence_len, 1])
                    prefix_reshaped = tf.cast(prefix_reshaped, tf.float32)
                    suffix_reshaped = tf.cast(suffix_reshaped, tf.float32)
                    self._hidden_state_size += 2
                    lstm_input = tf.concat([lstm_input, prefix_reshaped, suffix_reshaped], axis=2)
                elif self._orthographic_insertion_type == OrthographicInsertionType.EMBEDDED:
                    prefix_embedded = self.get_orthographic_embedding(self._prefix_features, self._prefix_orthographic_dim, "prefix_embedding")
                    suffix_embedded = self.get_orthographic_embedding(self._suffix_features, self._suffix_orthographic_dim, "suffix_embedding")
                    self._hidden_state_size += self._orthographic_hidden_state_size * 2
                    lstm_input = tf.concat([lstm_input, prefix_embedded, suffix_embedded], axis=2)

        ## Create forward and backward cell
        forward_cell = tf.contrib.rnn.LSTMCell(self._hidden_state_size, state_is_tuple=True)
        backward_cell = tf.contrib.rnn.LSTMCell(self._hidden_state_size, state_is_tuple=True)

        cell_fw2 = tf.contrib.rnn.LSTMCell(self._hidden_state_size, state_is_tuple=True)
        cell_bw2 = tf.contrib.rnn.LSTMCell(self._hidden_state_size, state_is_tuple=True)

        ## Apply bidrectional dyamic rnn to get a tuple of forward
        ## and backward outputs. Using dynamic rnn instead of just
        ## an rnn avoids the task of breaking the input into
        ## into a list of tensors (one per time step)
        with tf.variable_scope("lstm"):
            outputs, _ = tf.nn.bidirectional_dynamic_rnn(forward_cell, backward_cell,
                                                         lstm_input, dtype=tf.float32,
                                                         sequence_length=self._lengths)

        outputs = tf.concat(outputs, axis=-1)

        with tf.variable_scope("bi-TensorRNN2"):
            outputs, _ = tf.nn.bidirectional_dynamic_rnn(cell_fw2, cell_bw2, outputs,
                                                         sequence_length=self._lengths, dtype=tf.float32)

        outputs = tf.concat(outputs, axis=-1)

        with tf.variable_scope("bi-TensorRNN3"):
            outputs, _ = tf.nn.bidirectional_dynamic_rnn(cell_fw2, cell_bw2, outputs,
                                                         sequence_length=self._lengths, dtype=tf.float32)


        with tf.variable_scope("lstm_output"):
            ## concat forward and backward states
            outputs = tf.concat(outputs, 2)

            if orthographic_insertion_place == OrthographicInsertionPlace.OUTPUT:
                if self._orthographic_insertion_type == OrthographicInsertionType.ONE_HOT:
                    prefix_one_hot = tf.one_hot(self._prefix_features, self._prefix_orthographic_dim)
                    suffix_one_hot = tf.one_hot(self._suffix_features, self._suffix_orthographic_dim)
                    outputs = tf.concat([outputs, prefix_one_hot, suffix_one_hot], axis=2)
                elif self._orthographic_insertion_type == OrthographicInsertionType.INT_VALUES:
                    prefix_reshaped = tf.reshape(self._prefix_features, [BATCH_SIZE, self._sequence_len, 1])
                    suffix_reshaped = tf.reshape(self._suffix_features, [BATCH_SIZE, self._sequence_len, 1])
                    prefix_reshaped = tf.cast(prefix_reshaped, tf.float32)
                    suffix_reshaped = tf.cast(suffix_reshaped, tf.float32)
                    outputs = tf.concat([outputs, prefix_reshaped, suffix_reshaped], axis=2)
                elif self._orthographic_insertion_type == OrthographicInsertionType.EMBEDDED:
                    prefix_embedded = self.get_orthographic_embedding(self._prefix_features, self._prefix_orthographic_dim, "prefix_embedding")
                    suffix_embedded = self.get_orthographic_embedding(self._suffix_features, self._suffix_orthographic_dim, "suffix_embedding")
                    outputs = tf.concat([outputs, prefix_embedded, suffix_embedded], axis=2)

            # print outputs
            ## Apply linear transformation to get logits(unnormalized scores)
            logits = self.compute_logits(outputs)

            ## Get the normalized probabilities
            ## Note that this a rank 3 tensor
            ## It contains the probabilities of
            ## different POS tags for each batch
            ## example at each time step
            self._probabilities = tf.nn.softmax(logits)
            # print self._probabilities

            self._predicted_classes = tf.cast(tf.argmax(self._probabilities, dimension=2), tf.int32)


        self._loss = self.cost(self._output_tags, self._probabilities)
        self._average_loss = self._loss / tf.cast(self._total_length, tf.float32)
       # -----------------------------------------------
        self._accuracy = self.compute_accuracy(self._output_tags, self._probabilities, self._mask)
        self._average_accuracy = self._accuracy / tf.cast(self._total_length, tf.float32)

        self._oov_accuracy = self.compute_accuracy(self._output_tags, self._probabilities, self._oov_mask)
        self._average_oov_accuracy = self._oov_accuracy / tf.cast(self._total_oov_length,
                                                                  tf.float32) if self._total_oov_length != 0 else 1.0

    # Taken from https://github.com/monikkinom/ner-lstm/blob/master/model.py weight_and_bias function
    ## Creates a fully connected layer with the given dimensions and parameters
    def initialize_fc_layer(self, row_dim, col_dim, stddev=0.01, bias=0.1):
        weight = tf.truncated_normal([row_dim, col_dim], stddev=stddev)
        bias = tf.constant(bias, shape=[col_dim])
        return tf.Variable(weight, name='weight'), tf.Variable(bias, name='bias')

    # Taken from https://github.com/monikkinom/ner-lstm/blob/master/model.py __init__ function
    def compute_logits(self, outputs):
        softmax_input_size = int(outputs.get_shape()[2])
        outputs = tf.reshape(outputs, [-1, softmax_input_size])

        W, b = self.initialize_fc_layer(softmax_input_size, self._output_dim)

        logits = tf.matmul(outputs, W) + b
        logits = tf.reshape(logits, [-1, self._sequence_len, self._output_dim])
        return logits

    def add_loss_summary(self):
        tf.summary.scalar('Loss', self._average_loss)

    def add_accuracy_summary(self):
        tf.summary.scalar('Accuracy', self._average_accuracy)

    def add_oov_accuracy_summary(self):
        tf.summary.scalar('OOV Accuracy', self._average_oov_accuracy)

    # Taken from https://github.com/monikkinom/ner-lstm/blob/master/model.py __init__ function
    def get_train_op(self, loss, global_step):
        training_vars = tf.trainable_variables()
        grads, _ = tf.clip_by_global_norm(tf.gradients(loss, training_vars), 10)
        apply_gradient_op = self._optimizer.apply_gradients(zip(grads, training_vars),
                                                            global_step)
        return apply_gradient_op

    # Adapted from https://github.com/monikkinom/ner-lstm/blob/master/model.py cost function
    def compute_accuracy(self, pos_classes, probabilities, mask):
        predicted_classes = tf.cast(tf.argmax(probabilities, dimension=2), tf.int32)

        # predicted_classes = tf.multiply(predicted_classes, mask)
        # pos_classes = tf.multiply(pos_classes, mask)
        # # # y_true, y_pred weighted
        # f1_score = 0
        # # for i in range(128):
        # #     f1_score += sklearn.metrics.f1_score(pos_classes[i, :], predicted_classes[i, :], average="weighted")
        # return f1_score / 128

        correct_predictions = tf.cast(tf.equal(predicted_classes, pos_classes), tf.int32)
        correct_predictions = tf.multiply(correct_predictions, mask)
        return tf.cast(tf.reduce_sum(correct_predictions), tf.float32)


    def get_total_length(self):
        return self.total_length

    # Adapted from https://github.com/monikkinom/ner-lstm/blob/master/model.py cost function
    def cost(self, pos_classes, probabilities):
        pos_classes = tf.cast(pos_classes, tf.int32)
        pos_one_hot = tf.one_hot(pos_classes, self._output_dim)
        pos_one_hot = tf.cast(pos_one_hot, tf.float32)
        ## masking not needed since pos class vector will be zero for
        ## padded time steps
        cross_entropy = pos_one_hot * tf.log(probabilities)
        return -tf.reduce_sum(cross_entropy)

    @property
    def prefix_features(self):
        return self._prefix_features

    @property
    def suffix_features(self):
        return self._suffix_features

    @property
    def input_words(self):
        return self._input_words

    @property
    def hidden_size(self):
        return self._hidden_state_size

    @property
    def predicted_classes(self):
        return self._predicted_classes

    @property
    def mask(self):
        return self._mask

    @property
    def output_tags(self):
        return self._output_tags

    @property
    def loss(self):
        return self._loss

    @property
    def accuracy(self):
        return self._accuracy

    @property
    def total_length(self):
        return self._total_length

    @property
    def oov_accuracy(self):
        return self._oov_accuracy

    @property
    def total_oov_length(self):
        return self._total_oov_length


# Adapted from http://r2rt.com/recurrent-neural-networks-in-tensorflow-i.html
def generate_batch(X, y, P, S):
    for i in range(0, len(X), BATCH_SIZE):
        yield X[i:i + BATCH_SIZE], y[i:i + BATCH_SIZE], P[i:i + BATCH_SIZE], S[i:i + BATCH_SIZE]


def shuffle_data(X, y, P, S):
    ran = list(range(len(X)))
    # 将序列元素随机排序
    shuffle(ran)
    return [X[num] for num in ran], [y[num] for num in ran], [P[num] for num in ran], [S[num] for num in ran]


# Adapted from http://r2rt.com/recurrent-neural-networks-in-tensorflow-i.html
def generate_epochs(X, y, P, S, no_of_epochs):
    lx = len(X)
    lx = (lx // BATCH_SIZE) * BATCH_SIZE
    X = X[:lx]
    y = y[:lx]
    P = P[:lx]
    S = S[:lx]
    for i in range(no_of_epochs):
        shuffle_data(X, y, P, S)
        yield generate_batch(X, y, P, S)


## Compute overall loss and accuracy on dev/test data
def compute_summary_metrics(sess, m, sentence_words_val, sentence_tags_val, prefixes, suffixes):
    loss, accuracy, total_len, oov_accuracy, total_oov_len ,f1_score, numsteps= 0.0, 0.0, 0, 0.0, 0, 0.0, 0
    for i, epoch in enumerate(generate_epochs(sentence_words_val, sentence_tags_val, prefixes, suffixes, 1)):
        for step, (X, y, P, S) in enumerate(epoch):
            # batch_loss, batch_accuracy, batch_len, batch_oov_accuracy, batch_oov_len = \
            #     sess.run([m.loss, m.accuracy, m.total_length, m.oov_accuracy, m.total_oov_length], \
            #              feed_dict={m.input_words: X, m.output_tags: y, m.prefix_features: P, m.suffix_features: S})

            # request loss f1
            batch_loss, batch_accuracy, batch_len, batch_oov_accuracy, batch_oov_len, target, predicted, mask_out = \
                sess.run([m.loss, m.accuracy, m.total_length, m.oov_accuracy, m.total_oov_length, m.output_tags, m.predicted_classes, m.mask], \
                         feed_dict={m.input_words: X, m.output_tags: y, m.prefix_features: P, m.suffix_features: S})

            loss += batch_loss
            total_len += batch_len
            accuracy += batch_accuracy
            oov_accuracy += batch_oov_accuracy
            total_oov_len += batch_oov_len

            f1_score += compute_f1(target, predicted, mask_out)
            numsteps +=1

            # loss += batch_loss
            # accuracy += batch_accuracy
            # total_len += batch_len
            # oov_accuracy += batch_oov_accuracy
            # total_oov_len += batch_oov_len
    loss = loss / total_len if total_len != 0 else 0
    accuracy = accuracy / total_len if total_len != 0 else 1
    oov_accuracy = oov_accuracy / total_oov_len if total_oov_len != 0 else 1

    f1_score = f1_score / numsteps if numsteps !=0 else 1
    return loss, accuracy, oov_accuracy, f1_score

    # loss = loss / total_len if total_len != 0 else 0
    # f1_score = f1_score/numsteps if numsteps != 0 else 0
    # oov_accuracy = oov_accuracy / total_oov_len if total_oov_len != 0 else 1
    # return loss, f1_score, oov_accuracy


def compute_f1(target, predict_class, mask):
    predict_list = []
    target_list = []

    x_size, y_size = mask.shape

    for i in range(x_size):
        for j in range(y_size):
            if mask[i, j] == 1:
                predict_list.append(predict_class[i, j])
                target_list.append(target[i, j])

    return  sklearn.metrics.f1_score(target_list, predict_list, average="weighted")



## train and test adapted from https://github.com/tensorflow/tensorflow/blob/master/tensorflow/
## models/image/cifar10/cifar10_train.py and cifar10_eval.py
def train(sentence_words_train, sentence_tags_train, prefixes_train, suffixes_train, sentence_words_val,
          sentence_tags_val, prefixes_val, suffixes_val, vocab_size, prefix_size, suffix_size, no_pos_classes,
          train_dir, orthographic_insertion_place, orthographic_insertion_type, hidden_state_size):
    m = Model(vocab_size, prefix_size, suffix_size, MAX_LENGTH, no_pos_classes, orthographic_insertion_place, orthographic_insertion_type, hidden_state_size=hidden_state_size)
    with tf.Graph().as_default():
        global_step = tf.Variable(0, trainable=False)

        ## Add input/output placeholders
        m.create_placeholders()
        ## create the model graph
        m.create_graph()
        ## create training op
        train_op = m.get_train_op(m.loss, global_step)

        ## create saver object which helps in checkpointing
        ## the model
        saver = tf.train.Saver(tf.global_variables() + tf.local_variables())

        ## add scalar summaries for loss, accuracy
        m.add_accuracy_summary()
        m.add_oov_accuracy_summary()
        m.add_loss_summary()
        summary_op = tf.summary.merge_all()

        ## Initialize all the variables
        init = tf.global_variables_initializer()

        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        sess = tf.Session(config=config)
        sess.run(init)

        # print prefixes_train
        # print suffixes_train
        summary_writer = tf.summary.FileWriter(train_dir, sess.graph)
        j = 0
        for i, epoch in enumerate(generate_epochs(sentence_words_train, sentence_tags_train, prefixes_train, suffixes_train, NO_OF_EPOCHS)):
            start_time = time.time()
            with open('result_' + str(m.hidden_size) + '_f1_33. txt', 'a+') as f:
                for step, (X, y, P, S) in enumerate(epoch):
                    _, summary_value = sess.run([train_op, summary_op], feed_dict=
                    {m.input_words: X, m.output_tags: y, m.prefix_features: P, m.suffix_features: S})


                    duration = time.time() - start_time
                    j += 1
                    if j % VALIDATION_FREQUENCY == 0:
                        val_loss, val_accuracy, val_oov_accuracy, val_f1_score = compute_summary_metrics(sess, m, sentence_words_val,
                                                                                           sentence_tags_val, prefixes_val, suffixes_val)
                        summary = tf.Summary()
                        summary.ParseFromString(summary_value)
                        summary.value.add(tag='Validation Loss', simple_value=val_loss)
                        summary.value.add(tag='Validation Accuracy', simple_value=val_accuracy)
                        summary.value.add(tag='Validation OOV Accuracy', simple_value=val_oov_accuracy)

                        summary.value.add(tag='Validation f1', simple_value=val_f1_score)

                        summary_writer.add_summary(summary, j)
                        log_string = '{} batches ====> Validation Accuracy {:.3f}, Validation f1 {:.3f}, Validation OOV Accuracy {:.3f}, Validation Loss {:.3f}'
                        print(get_time_string(), log_string.format(j, val_accuracy, val_f1_score, val_oov_accuracy, val_loss))
                        f.write(log_string.format(j, val_accuracy, val_f1_score, val_oov_accuracy, val_loss) + '\r\n')
                    else:
                        summary_writer.add_summary(summary_value, j)

                    if j % CHECKPOINT_FREQUENCY == 0:
                        checkpoint_path = os.path.join(train_dir, 'model.ckpt')
                        saver.save(sess, checkpoint_path, global_step=j)


## Check performance on held out test data
## Loads most recent model from train_dir
## and applies it on test data
def test(sentence_words_test, sentence_tags_test, prefixes_test, suffixes_test,
         vocab_size, prefix_size, suffix_size, no_pos_classes, train_dir,
         orthographic_insertion_place, orthographic_insertion_type, hidden_size):
    m = Model(vocab_size, prefix_size, suffix_size, MAX_LENGTH, no_pos_classes, orthographic_insertion_place, orthographic_insertion_type, hidden_state_size=hidden_size)
    with tf.Graph().as_default():
        global_step = tf.Variable(0, trainable=False)
        m.create_placeholders()
        m.create_graph()
        saver = tf.train.Saver(tf.global_variables())
        with tf.Session() as sess:
            ckpt = tf.train.get_checkpoint_state(train_dir)
            if ckpt and ckpt.model_checkpoint_path:
                saver.restore(sess, ckpt.model_checkpoint_path)

                global_step = ckpt.model_checkpoint_path.split('/')[-1].split('-')[-1]
            test_loss, test_accuracy, test_oov_accuracy, test_f1_score = compute_summary_metrics(sess, m, sentence_words_test,
                                                                                  sentence_tags_test, prefixes_test, suffixes_test)
            print(get_time_string(), 'Test Accuracy: {:.3f}'.format(test_accuracy))
            print(get_time_string(), 'Test f1: {:.3f}'.format(test_f1_score))
            print(get_time_string(), 'Test OOV Accuracy: {:.3f}'.format(test_oov_accuracy))
            print(get_time_string(), 'Test Loss: {:.3f}'.format(test_loss))


if __name__ == '__main__':
    dataset_path = sys.argv[1]
    train_dir = sys.argv[2]
    split_type = sys.argv[3]
    experiment_type = sys.argv[4]

    hidden_size = int(sys.argv[5])


    orthographic_insertion_place = 'NONE' if len(sys.argv) <= 6 else sys.argv[6]
    orthographic_insertion_type = 'ONE_HOT' if len(sys.argv) <= 7 else sys.argv[7]

    # print OrthographicInsertionPlace[orthographic_insertion_place]
    # OrthographicInsertionType orthographic_insertion_type(OrthographicInsertionType.NONE)
    orthographic_insertion_place = OrthographicInsertionPlace[orthographic_insertion_place]
    orthographic_insertion_type = OrthographicInsertionType[orthographic_insertion_type]
    # print orthographic_insertion_place
    # print orthographic_insertion_type

    # if orthographic_insertion_place == 'NONE':
    #     orthographic_insertion_place = OrthographicInsertionPlace.NONE
    # elif orthographic_insertion_place == 'INPUT':
    #     orthographic_insertion_place = OrthographicInsertionPlace.INPUT
    # elif orthographic_insertion_place == 'OUTPUT':
    #     orthographic_insertion_place = OrthographicInsertionPlace.OUTPUT
    # else:
    #     print 'Invalid orthographic insertion type! Please specify NONE/INPUT/OUTPUT.'
    #     exit(1)

    p = PreprocessData(dataset_type='wsj')

    files = p.preProcessDirectory(dataset_path)

    if split_type == 'standard':
        train_files, val_files, test_files = p.get_standard_split(files)
    else:
        shuffle(files)
        train_files, test_val_files = p.split_data(files, 0.8)
        test_files, val_files = p.split_data(test_val_files, 0.5)

    train_mat = p.get_raw_data(train_files, 'train')
    val_mat = p.get_raw_data(val_files, 'validation')
    test_mat = p.get_raw_data(test_files, 'test')

    X_train, y_train, P_train, S_train, _ = p.get_processed_data(train_mat, MAX_LENGTH)
    X_val, y_val, P_val, S_val, _ = p.get_processed_data(val_mat, MAX_LENGTH)

    X_test, y_test, P_test, S_test, _ = p.get_processed_data(test_mat, MAX_LENGTH)

    if experiment_type == 'train':
        if os.path.exists(train_dir):
            shutil.rmtree(train_dir)
        os.mkdir(train_dir)
        print(len(p.pos_tags))
        print(len(p.vocabulary))

        train(X_train, y_train, P_train, S_train, X_val, y_val, P_val, S_val, len(p.vocabulary) + 2, len(p.prefix_orthographic), len(p.suffix_orthographic), len(p.pos_tags) + 1, train_dir, orthographic_insertion_place, orthographic_insertion_type, hidden_size)
    else:
        test(X_test, y_test, P_test, S_test, len(p.vocabulary) + 2, len(p.prefix_orthographic), len(p.suffix_orthographic), len(p.pos_tags) + 1, train_dir, orthographic_insertion_place, orthographic_insertion_type, hidden_size)
