import tensorflow as tf
import numpy as np
import util.vocabmapping
import pickle
import math
from glove import *


class WSDModel(object):
	def __init__(self, vocab_size, hidden_size, dropout,
	num_layers, max_gradient_norm, max_seq_length,
	learning_rate, lr_decay,batch_size, word, num_classes, forward_only=False):
		self.num_classes = num_classes
		self.word = word
		self.vocab_size = vocab_size
		self.learning_rate = tf.Variable(float(learning_rate), trainable=False)
		self.learning_rate_decay_op = self.learning_rate.assign(
		self.learning_rate * lr_decay)
		initializer = tf.random_uniform_initializer(-1,1)
		self.batch_pointer = 0
		self.seq_input = []
		self.batch_size = batch_size
		self.seq_lengths = []
		self.projection_dim = hidden_size

		self.filter_width = 3
		self.num_filter = 30
		

		self.dropout = dropout
		self.max_gradient_norm = max_gradient_norm
		self.global_step = tf.Variable(0, trainable=False)
		self.max_seq_length = max_seq_length
		with open('util/vocab_'+self.word+'_sentences.txt', 'rb') as handle:
			init_word_vecs = pickle.load(handle)

		init_emb = fill_with_gloves(init_word_vecs, 100)
	
	
		self.str_summary_type = tf.placeholder(tf.string,name="str_summary_type")
		self.seq_input = tf.placeholder(tf.int32, shape=[None, max_seq_length],
		name="input")
		
		self.target = tf.placeholder(tf.float32, name="target", shape=[None,self.num_classes+1])
		self.seq_lengths = tf.placeholder(tf.int32, shape=[None],
		name="early_stop")

		self.dropout_keep_prob_embedding = tf.constant(self.dropout)
		self.dropout_keep_prob_lstm_input = tf.constant(self.dropout)
		self.dropout_keep_prob_lstm_output = tf.constant(self.dropout)
		
		
		def embedding_initializer(vec, dtype,partition_info=None):
			return init_emb if init_emb is not None else tf.random_uniform([vocab_size, 100],-.1, .1, dtype)
		with tf.variable_scope("embedding"):
			W = tf.get_variable(
				"W",
				[self.vocab_size, 100],
				initializer=embedding_initializer)
			
			embedded_tokens = tf.nn.embedding_lookup(W, self.seq_input)
			
			embedded_tokens_drop = tf.nn.dropout(embedded_tokens, self.dropout_keep_prob_embedding)
			

			embedding_weights = tf.summary.histogram('embedding_weight_update ', W)

		#rnn_input = [embedded_tokens_drop[:, i, :] for i in range(self.max_seq_length)]
		rnn_input = tf.convert_to_tensor([embedded_tokens_drop[:, i, :] for i in range(self.max_seq_length)])
		rnn_input = tf.reshape(rnn_input,[-1,self.max_seq_length,100])
		
		self.word_embeddings = tf.expand_dims(rnn_input, 1)
		print(self.word_embeddings)

		with tf.variable_scope("idcnn"):
			filter_weights = tf.get_variable(
					"idcnn_filter",
					shape=[1, self.filter_width, 100,
						1*self.num_filter])

			layerInput = tf.nn.conv2d(self.word_embeddings,
								filter_weights,
								strides=[1, 1, 1, 1],
								padding="SAME",
								name="init_layer")
			filter_weight = tf.summary.histogram('filter_weight ', W)
			dilation = 1
			print(layerInput)
			W1 = tf.get_variable(
				"filterW",
				shape=[1, self.filter_width, 1*self.num_filter,
						2*self.num_filter],
				initializer=tf.contrib.layers.xavier_initializer())
			

			b = tf.get_variable("filterB", shape=[2*self.num_filter])
			conv = tf.nn.atrous_conv2d(layerInput,
							W1,
							rate=dilation,
							padding="SAME")
			conv = tf.nn.bias_add(conv, b)
			conv = tf.nn.relu(conv)
			W1 = tf.summary.histogram('CNN-layer1 ', W1)
			print(conv)
			W2 = tf.get_variable(
				"filterW1",
				shape=[1, self.filter_width, 2*self.num_filter, 2*hidden_size],#cnn_base
				initializer=tf.contrib.layers.xavier_initializer()
				)
			
			b1 = tf.get_variable("filterB1", shape=[2*hidden_size])
			conv1 = tf.nn.atrous_conv2d(conv,
							W2,
							rate=dilation,
							padding="SAME")
			conv1 = tf.nn.bias_add(conv1, b1)
			conv1 = tf.nn.relu(conv1)
			W2 = tf.summary.histogram('CNN-layer2 ', W2)
			#conv1 = tf.transpose(conv1,perm=[2,1,0,3])
			rnn_concatenated_state = conv1
			print(rnn_concatenated_state[-1][0])

		with tf.variable_scope("output_projection"):
			W = tf.get_variable(
				"W",
				[100, self.num_classes+1],
				initializer=tf.truncated_normal_initializer(stddev=0.1))
			b = tf.get_variable(
				"b",
				[self.num_classes+1],initializer = tf.constant_initializer(0.1))

			self.scores = tf.nn.xw_plus_b(tf.transpose(rnn_concatenated_state[-1][0]), W, b)
			self.y = tf.nn.softmax(self.scores)
			self.predictions = tf.argmax(self.scores, 1)
			self.modified_targets = tf.argmax(self.target, 1)

			output_weight = tf.summary.histogram('output_projection_weights ', W)



		with tf.variable_scope("loss"):
			self.losses = tf.nn.softmax_cross_entropy_with_logits(logits=self.scores, labels=self.target, name="ce_losses")
			self.total_loss = tf.reduce_sum(self.losses)
			self.mean_loss = tf.reduce_mean(self.losses)

		with tf.variable_scope("accuracy"):
			
			self.correct_predictions = tf.equal(self.predictions, tf.argmax(self.target, 1))
			self.accuracy = tf.reduce_mean(tf.cast(self.correct_predictions, tf.float32), name="accuracy")


		params = tf.trainable_variables()
		if not forward_only:
			with tf.name_scope("train") as scope:
				opt = tf.train.AdamOptimizer(self.learning_rate)
			gradients = tf.gradients(self.losses, params)
			clipped_gradients, norm = tf.clip_by_global_norm(gradients, self.max_gradient_norm)
			with tf.name_scope("grad_norms") as scope:
				grad_summ = tf.summary.scalar("grad_norms", norm)
			self.update = opt.apply_gradients(zip(clipped_gradients, params), global_step=self.global_step)
			loss_summ = tf.summary.scalar("{0}_loss".format(self.str_summary_type), self.mean_loss)
			acc_summ = tf.summary.scalar("{0}_accuracy".format(self.str_summary_type), self.accuracy)
			#new addition to view weights
			self.merged = tf.summary.merge([loss_summ, acc_summ,output_weight,W2,W1,filter_weight,embedding_weights])
		self.saver = tf.train.Saver()
		tf.global_variables_initializer().run()


	def getBatch(self, test_data=False):
		if not test_data:
			batch_inputs = self.train_data[self.train_batch_pointer]#.transpose()
		
			targets = self.train_targets[self.train_batch_pointer]
			seq_lengths = self.train_sequence_lengths[self.train_batch_pointer]
			self.train_batch_pointer += 1
			self.train_batch_pointer = self.train_batch_pointer % len(self.train_data)
			return batch_inputs, targets, seq_lengths
		else:
			batch_inputs = self.test_data[self.test_batch_pointer]#.transpose()
		
			targets = self.test_targets[self.test_batch_pointer]
			seq_lengths = self.test_sequence_lengths[self.test_batch_pointer]
			self.test_batch_pointer += 1
			self.test_batch_pointer = self.test_batch_pointer % len(self.test_data)
			return batch_inputs, targets, seq_lengths

	def initData(self, data, train_start_end_index, test_start_end_index):
		'''
		Split data into train/test sets and load into memory
		'''
		self.train_batch_pointer = 0
		self.test_batch_pointer = 0
	
		targets = (data.transpose()[-2]).transpose()
	
		#+1 for unknown class to remedy padding
		onehot = np.zeros((len(targets), self.num_classes+1))
		onehot[np.arange(len(targets)), targets] = 1
		sequence_lengths = (data.transpose()[-1]).transpose()
		data = (data.transpose()[0:-2]).transpose()
		self.train_data = data[train_start_end_index[0]: train_start_end_index[1]+1]
		self.test_data = data[test_start_end_index[0]:test_start_end_index[1]+1]
		pad_amount = int(math.ceil(len(self.test_data)/float(self.batch_size)))*self.batch_size-len(self.test_data)
		auxiliary_test_data = np.zeros([pad_amount,100+2],dtype=np.int32)
		auxiliary_test_data.fill(util.vocabmapping.VocabMapping(self.word).getIndex("<PAD>"))
		auxiliary_test_data[:,100] = self.num_classes
		auxiliary_test_data[:,101] = 100
		test_data = (auxiliary_test_data.transpose()[0:-2]).transpose()
		test_data_sequence_length = (auxiliary_test_data.transpose()[-1]).transpose()
		test_data_target = (auxiliary_test_data.transpose()[-2]).transpose()
		targets = np.append(targets,test_data_target,axis=0)
		onehot = np.zeros((len(targets),self.num_classes+1))
		onehot[np.arange(len(targets)),targets]=1
		data_len = len(data)
		self.test_data = np.append(self.test_data, test_data,axis=0)
		num_train_batches = len(self.train_data) // self.batch_size
		num_test_batches = len(self.test_data) // self.batch_size
		train_cutoff = len(self.train_data) - (len(self.train_data) % self.batch_size)
		self.train_data = self.train_data[:train_cutoff]
		self.train_sequence_lengths = sequence_lengths[train_start_end_index[0]:train_start_end_index[1]][:train_cutoff]
		self.train_sequence_lengths = np.split(self.train_sequence_lengths, num_train_batches)
		self.train_targets = onehot[train_start_end_index[0]:train_start_end_index[1]][:train_cutoff]
		self.train_targets = np.split(self.train_targets, num_train_batches)
		self.train_data = np.split(self.train_data, num_train_batches)

		print("Test size is: {0}, splitting into {1} batches".format(len(self.test_data), num_test_batches))
		self.test_data = np.split(self.test_data, num_test_batches)
		self.test_targets = onehot[test_start_end_index[0]:onehot.shape[0]]
		self.test_targets = np.split(self.test_targets, num_test_batches)
		self.test_sequence_lengths = sequence_lengths[test_start_end_index[0]:test_start_end_index[1]+1]
		self.test_sequence_lengths = np.append(self.test_sequence_lengths,test_data_sequence_length)
		self.test_sequence_lengths = np.split(self.test_sequence_lengths, num_test_batches)

	def step(self, session, inputs, targets, seq_lengths, forward_only=False):
		input_feed = {}
		input_feed[self.seq_input.name] = inputs
		input_feed[self.target.name] = targets
		input_feed[self.seq_lengths.name] = seq_lengths
		if not forward_only:
			input_feed[self.str_summary_type.name] = "train"
			output_feed = [self.merged, self.mean_loss, self.update]
		else:
			input_feed[self.str_summary_type.name] = "test"
			output_feed = [self.merged, self.mean_loss, self.y, self.accuracy,self.predictions,self.modified_targets]
		outputs = session.run(output_feed, input_feed)

		if not forward_only:
			return outputs[0], outputs[1], None
		else:
			return outputs[0], outputs[1], outputs[2], outputs[3],outputs[4],outputs[5]