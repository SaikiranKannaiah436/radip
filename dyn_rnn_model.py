import tensorflow as tf
import numpy as np
import random
import MDN
from tensorflow.python.ops import nn_ops
#from TF_mods import basic_rnn_seq2seq_with_loop_function
from tensorflow.contrib.legacy_seq2seq.python.ops import seq2seq
from tensorflow.python.framework import ops
from tensorflow.python.ops import clip_ops
import tensorflow.contrib.layers
from recurrent_batchnorm_tensorflow.BN_LSTMCell import BN_LSTMCell


class DynamicRnnSeq2Seq(object):

    def __init__(self, parameters,hyper_search=False):
        #feed_future_data, train, num_observation_steps, num_prediction_steps, batch_size,
        #         rnn_size, num_layers, learning_rate, learning_rate_decay_factor, input_size, max_gradient_norm,
        #        dropout_prob,random_bias,subsample,random_rotate,num_mixtures,model_type):

        # feed_future_data: whether or not to feed the true data into the decoder instead of using a loopback
        #                function. If false, a loopback function is used, feeding the last generated output as the next
        #                decoder input.
        # train: train the model (or test)
        # Subsample: amount of subsampling. IMPORTANT If this is non-one, the input array must be n times longer than usual, as it only subsamples down
        # This is so that the track is not subsampled the same way each track sample.

        #######################################
        # The LSTM Model consists of:
        # Input Linear layer
        # N LSTM layers
        # a linear output layer to convert the LSTM output to MDN format
        #
        # MDN Format:
        # pi mu1 mu2 sigma1 sigma2 rho
        # (repeat for n mixtures)
        #
        # This MDN format is then either used for the loss, or is sampled to get a real value

        #TODO Reorganise code using namespace for better readability
        self.parameters = parameters
        self.max_gradient_norm = parameters['max_gradient_norm']
        self.rnn_size = parameters['rnn_size']
        self.num_layers = parameters['num_layers']
        dtype = tf.float32

        self.batch_size = parameters['batch_size']
        self.input_size = parameters['input_size']
        self.embedding_size = parameters['embedding_size']
        self.observation_steps = parameters['observation_steps']
        self.prediction_steps = parameters['prediction_steps']
        self.dropout_prob = parameters['dropout_prob']
        self.random_bias = parameters['random_bias']
        self.subsample = parameters['subsample']
        self.random_rotate = parameters['random_rotate']
        self.num_mixtures = parameters['num_mixtures']
        self.model_type = parameters['model_type']
        self.num_classes = parameters['num_classes']
        self.global_step = tf.Variable(0, trainable=False,name="Global_step")

        self.learning_rate = tf.Variable(float(parameters['learning_rate']), trainable=False, name="Learning_rate")
        min_rate = parameters['learning_rate'] * 0.001
        self.learning_rate_decay_op = self.learning_rate.assign(
            (parameters['learning_rate'] - min_rate) *
            (parameters['learning_rate_decay_factor']**tf.cast(self.global_step,tf.float32) + min_rate))
        self.network_summaries = []
        keep_prob = 1-self.dropout_prob

        # Feed future data is to be used during sequence generation. It allows real data to be passed at times t++
        # instead of the generated output. For training only, I may not use it at all.
        feed_future_data = parameters['feed_future_data']

        if parameters['model_type'] == 'classifier' and self.prediction_steps > 1:
            raise Exception("Error. Classifier model can only have 1 prediction step")

        #if feed_future_data and not train:
        #    print "Warning, feeding the model future sequence data (feed_forward) is not recommended when the model is not training."

        # The output of the multiRNN is the size of rnn_size, and it needs to match the input size, or loopback makes
        #  no sense. Here a single layer without activation function is used, but it can be any number of
        #  non RNN layers / functions
        if self.model_type == 'MDN':
            n_out = 6*self.num_mixtures
        if self.model_type=='classifier':
            n_out = parameters['num_classes']

        ############## LAYERS ###################################

        # Layer is linear, just to re-scale the LSTM outputs [-1,1] to [-9999,9999]
        # If there is a regularizer, these weights should be excluded?

        with tf.variable_scope('output_proj'):
            o_w = tf.get_variable("proj_w", [self.rnn_size, n_out],
                                  initializer=tf.truncated_normal_initializer(stddev=1.0 / np.sqrt(self.embedding_size)))
            o_b = tf.get_variable("proj_b", [n_out],
                                  initializer=tf.constant_initializer(0.1))
            output_projection = (o_w, o_b)

        with tf.variable_scope('input_scaling'):
            i_s_m = tf.get_variable('in_scale_mean', shape=[self.input_size],trainable=False,initializer=tf.zeros_initializer())
            i_s_s = tf.get_variable('in_scale_stddev', shape=[self.input_size],trainable=False,initializer=tf.ones_initializer())
            scaling_layer = (i_s_m,i_s_s)
            self.scaling_layer = scaling_layer
        with tf.variable_scope('input_embedding_layer'):
            i_w = tf.get_variable("in_w", [self.input_size, self.embedding_size], # Remember, batch_size is automatic
                                  initializer=tf.truncated_normal_initializer(stddev=1.0/np.sqrt(self.embedding_size)))
            i_b = tf.get_variable("in_b", [self.embedding_size],
                                  initializer=tf.constant_initializer(0.1))
            input_layer = (i_w, i_b)

        # def _generate_rnn_layer():
        #     if parameters['RNN_cell'] == "LSTMCell":
        #         return tf.contrib.rnn.DropoutWrapper(
        #                         tf.contrib.rnn.LSTMCell(self.rnn_size,state_is_tuple=True,
        #                                                 use_peepholes=parameters['peephole_connections'])
        #                         ,output_keep_prob=keep_prob)
        #     if parameters['RNN_cell'] == "BN_LSTMCell":
        #         return tf.contrib.rnn.DropoutWrapper(
        #                         BN_LSTMCell(self.rnn_size,is_training=True,
        #                                                 use_peepholes=parameters['peephole_connections'])
        #                         ,output_keep_prob=keep_prob)

        #
        # if self.num_layers > 1:
        #     self._RNN_layers = tf.contrib.rnn.MultiRNNCell([_generate_rnn_layer() for _ in range(self.num_layers)],state_is_tuple=True)
        # else:
        #     self._RNN_layers = _generate_rnn_layer()

        # Don't double dropout
        #self._RNN_layers = tensorflow.contrib.rnn.DropoutWrapper(self._RNN_layers,output_keep_prob=keep_prob)

        def output_function(output):
            return tf.nn.dropout(
                tf.nn.relu(
                    nn_ops.xw_plus_b(
                        output, output_projection[0], output_projection[1],name="output_projection"
                    )
                )
                , 0.5)

        def _condition_sampled_output(MDN_samples):
            # Simple hack for now as I cannot get t-1 data for t_0 derivatives easily due to scoping problems.
            # sampled has shape 256,2 - it needs 256,4
            if MDN_samples.shape[1] < scaling_layer[0].shape[0]:
                resized = tf.concat([MDN_samples, tf.zeros(
                    [MDN_samples.shape[0], scaling_layer[0].shape[0] - MDN_samples.shape[1]], dtype=tf.float32)], 1)
            else:
                resized = MDN_samples
            upscaled = tf.add(tf.multiply(resized, scaling_layer[1]), scaling_layer[0])
            return upscaled

        def _apply_scaling_and_input_layer(input_data):
            return tf.nn.dropout(tf.nn.relu(
                                            nn_ops.xw_plus_b(
                                                tf.divide(
                                                    tf.subtract(
                                                        input_data, scaling_layer[0]),
                                                    scaling_layer[1]),  # Input scaling
                                                input_layer[0], input_layer[1])),
                                        1-parameters['embedding_dropout'])
        #The loopback function needs to be a sampling function, it does not generate loss.
        # def simple_loop_function(prev, i):
        #     '''function that loops the data from the output of the LSTM to the input
        #     of the LSTM for the next timestep. So it needs to apply the output layers/function
        #     to generate the data at that timestep, and then'''
        #     # I might need to do some hacking with i.
        #     if output_projection is not None:
        #         #Output layer
        #         prev = output_function(prev)
        #     if self.model_type == 'MDN':
        #         # Sample to generate output
        #         sampled = MDN.sample(prev)
        #         prev = _condition_sampled_output(sampled)
        #         # prev = MDN.compute_derivates(prev,new,parameters['input_columns'])
        #
        #     # Apply input layer
        #     prev = _apply_scaling_and_input_layer(prev)
        #     return prev

        # The seq2seq function: we use embedding for the input and attention.
        # def seq2seq_f(encoder_inputs, decoder_inputs, feed_forward):
        #     if not feed_forward: #feed last output as next input
        #         loopback_function = simple_loop_function
        #     else:
        #         loopback_function = None #feed correct input
        #     #return basic_rnn_seq2seq_with_loop_function(encoder_inputs,decoder_inputs,cell,
        #     #                                                         loop_function=loopback_function,dtype=dtype)
        #     return seq2seq.tied_rnn_seq2seq(encoder_inputs,decoder_inputs,self._RNN_layers,
        #                                     loop_function=loopback_function,dtype=dtype)


        ################# FEEDS SECTION #######################
        # Feeds for inputs.
        self.observation_inputs = []
        self.future_inputs = []
        self.target_weights = []
        targets = []
        targets_sparse = []

        for i in xrange(self.observation_steps):  # Last bucket is the biggest one.
            self.observation_inputs.append(tf.placeholder(tf.float32, shape=[self.batch_size, self.input_size],
                                                          name="observation{0}".format(i)))

        if self.model_type == 'MDN':
            for i in xrange(self.prediction_steps):
                self.future_inputs.append(tf.placeholder(tf.float32, shape=[self.batch_size, self.input_size],
                                                         name="prediction{0}".format(i)))
            for i in xrange(self.prediction_steps):
                self.target_weights.append(tf.placeholder(dtype, shape=[self.batch_size],
                                                        name="weight{0}".format(i)))
            #targets are just the future data
            # Rescale gt data x1 and x2 such that the MDN is judged in smaller unit scale dimensions
            # This is because I do not expect the network to figure out the scaling, and so the Mixture is in unit size scale
            # So the GT must be brought down to meet it.
            targets\
                = [tf.divide(tf.subtract(self.future_inputs[i], scaling_layer[0]), scaling_layer[1])
                   for i in xrange(len(self.future_inputs))]


            #targets = tf.divide(tf.subtract(targets_full_scale, scaling_layer[0]), scaling_layer[1])

        if self.model_type == 'classifier':
            # Add a single target. Name is target0 for continuity
            target = tf.placeholder(tf.int32, shape=[self.batch_size, self.num_classes],
                                                         name="target".format(i))
            targets_sparse.append(tf.squeeze(tf.argmax(target,1),name="Sq_"+target.op.name))
            self.target_weights.append(tf.ones([self.batch_size],name="weight".format(i)))
            targets = [target]

        #Hook for the input_feed
        self.target_inputs = targets

        #Leave the last observation as the first input to the decoder
        #self.encoder_inputs = self.observation_inputs[0:-1]
        with tf.variable_scope('encoder_inputs'):
            self.encoder_inputs = [_apply_scaling_and_input_layer(input_timestep)
                                   for input_timestep in self.observation_inputs[0:-1]]

        #decoder inputs are the last observation and all but the last future
        with tf.variable_scope('decoder_inputs'):
            self.decoder_inputs = [_apply_scaling_and_input_layer(self.observation_inputs[-1])]

        # Todo should this have the input layer applied?
            self.decoder_inputs.extend([_apply_scaling_and_input_layer(self.future_inputs[i])
                                        for i in xrange(len(self.future_inputs) - 1)])

        with tf.variable_scope('seq_rnn'):
            self.LSTM_output, self.internal_states = seq2seq_f(self.encoder_inputs, self.decoder_inputs, feed_future_data)

        # self.outputs is a list of len(prediction_steps) containing [size batch x rnn_size]
        # The output projection below reduces this to:
        #                 a list of len(prediction_steps) containing [size batch x input_size]
        # BUG This is incorrect -- technically.
        # Because MDN.sample() is a random function, this sample is not the
        # sample being used in the loopback function.
        if output_projection is not None:
            self.model_output = [output_function(output) for output in self.LSTM_output]
        else:
            self.model_output = self.LSTM_output
        if self.model_type == 'MDN':
            self.MDN_sampled_output = [_condition_sampled_output(MDN.sample(x))  # Apply output scaling
                                       for x in self.model_output]

        def mse(x, y):
            return tf.sqrt(tf.reduce_mean(tf.square(tf.subtract(y, x))))


########### EVALUATOR / LOSS SECTION ###################
        # TODO There are several types of cost functions to compare tracks. Implement many
        # Mainly, average MSE over the whole track, or just at a horizon time (t+10 or something)
        # There's this corner alg that Social LSTM refernces, but I haven't looked into it.
        # NOTE - there is a good cost function for the MDN (MLE), this is different to the track accuracy metric (above)
        if self.model_type == 'MDN':
            self.losses = tf.contrib.legacy_seq2seq.sequence_loss(self.model_output, targets, self.target_weights,
                                                      #softmax_loss_function=lambda x, y: mse(x,y))
                                                  softmax_loss_function=MDN.lossfunc_wrapper)
            self.losses = self.losses / self.batch_size
            self.accuracy = -self.losses #TODO placeholder, use MSE or something visually intuitive
        if self.model_type == 'classifier':
            #embedding_regularizer = tf.reduce_sum(tf.abs(i_w),name="Embedding_L1_reg") # Only regularize embedding layer
            embedding_regularizer = tf.contrib.layers.l1_regularizer(parameters['reg_embedding_beta'])
            reg_loss = tf.contrib.layers.apply_regularization(embedding_regularizer,[i_w])
            # Don't forget that sequence loss uses sparse targets
            l2_reg_list = [o_w]
            # So there should be L2 applied to the recurrent weights, and the input weights... maybe. -- hyperparam this.
            # I assume multi_rnn_cell/cell_0/lstm_cell/weights is the recurrent, and w_i_diag is the input weights
            if parameters['l2_recurrent_decay']:
                l2_reg_list.extend([x for x in tf.trainable_variables() if ('weight' in x.name) and ('rnn_cell') in x.name])
            if parameters['l2_lstm_input_decay']:
                l2_reg_list.extend([x for x in tf.trainable_variables() if ('w_i_diag' in x.name) and ('rnn_cell') in x.name])

            embedding_regularizer = tf.contrib.layers.l2_regularizer(parameters['l2_reg_beta'])
            reg_loss += tf.contrib.layers.apply_regularization(embedding_regularizer, l2_reg_list)

            self.losses = (tf.contrib.legacy_seq2seq.sequence_loss(self.model_output, targets_sparse, self.target_weights)
                           + reg_loss)

            #TODO I have to take into account padding here - not a huge issue as I do take it into account in the report
            # and there is no padding during training.
            #squeeze away output to remove a single element list (It would be longer if classifier was allowed 2+ timesteps
            correct_prediction = tf.equal(tf.argmax(tf.squeeze(self.model_output), 1), targets_sparse,
                                          name="Correct_prediction")
            self.accuracy = tf.reduce_mean(tf.cast(correct_prediction, tf.float32),name="Accuracy")


############# OPTIMIZER SECTION ########################
        # Gradients and SGD update operation for training the model.
        tvars = tf.trainable_variables()
        #if train:
        # I don't see the difference here, as during testing the updates are not run
        self.gradient_norms = []
        self.updates = []
        #opt = tf.train.AdadeltaOptimizer(self.learning_rate)
        opt = tf.train.AdamOptimizer(self.learning_rate)
        #opt = tf.train.RMSPropOptimizer(self.learning_rate)
        #opt = tf.train.GradientDescentOptimizer(self.learning_rate)
        gradients = tf.gradients(self.losses, tvars)
        clipped_gradients, norm = tf.clip_by_global_norm(gradients, self.max_gradient_norm)

        self.gradient_norms.append(norm)

        gradients = zip(clipped_gradients, tvars)
        self.updates.append(opt.apply_gradients(
            gradients, global_step=self.global_step))

############# LOGGING SECTION ###########################
        for gradient, variable in gradients:  #plot the gradient of each trainable variable
            if variable.name.find("seq_rnn/combined_tied_rnn_seq2seq/tied_rnn_seq2seq/MultiRNNCell") == 0:
                var_log_name = variable.name[64:] #Make the thing readable in Tensorboard
            else:
                var_log_name = variable.name
            if isinstance(gradient, ops.IndexedSlices):
                grad_values = gradient.values
            else:
                grad_values = gradient
            if not hyper_search:
                self.network_summaries.append(
                    tf.summary.histogram(var_log_name, variable))
                self.network_summaries.append(
                    tf.summary.histogram(var_log_name + "/gradients", grad_values))
                self.network_summaries.append(
                    tf.summary.histogram(var_log_name + "/gradient_norm", clip_ops.global_norm([grad_values])))

        self.network_summaries.append(tf.summary.scalar('Loss', self.losses))
        self.network_summaries.append(tf.summary.scalar('Learning Rate', self.learning_rate))

        self.summary_op = tf.summary.merge(self.network_summaries)

        self.saver = tf.train.Saver(max_to_keep=99999)

        return

    def set_normalization_params(self,session,encoder_means,encoder_stddev):
        # # Function that manually sets the scaling layer for use in input normalization
        # session.run(tf.assign(self.scaling_layer[0], encoder_means))
        # session.run(tf.assign(self.scaling_layer[1], encoder_stddev))

        session.run(self.scaling_layer[0].assign(encoder_means))
        session.run(self.scaling_layer[1].assign(encoder_stddev))

        return

    def step(self, session, observation_inputs, future_inputs, target_weights, train_model, summary_writer=None):
        """Run a step of the model feeding the given inputs.
        Args:
          session: tensorflow session to use.
          observation_inputs: list of numpy int vectors to feed as encoder inputs.
          future_inputs: list of numpy float vectors to be used as the future path if doing a path prediction
          target_weights: list of numpy float vectors to feed as target weights.
          train_model: whether to do the backward step or only forward.
        Returns:
          A triple consisting of gradient norm (or None if we did not do backward),
          average perplexity, and the outputs.
        Raises:
          ValueError: if length of encoder_inputs, decoder_inputs, or
            target_weights disagrees with bucket size for the specified bucket_id.
        """

        ## Batch Norm Changes
        # The cell should be a drop in replacement above.
        # The tricky part here is that I need to update the state: BN_LSTM.is_training = train_model
        # I should be able to loop over all BN_LSTM Cells in the graph somehow.
        if self.parameters['RNN_cell'] == "BN_LSTMCell":
            for dropout_cell in self._RNN_layers._cells:
                dropout_cell._cell.is_training = train_model

        # Input feed: encoder inputs, decoder inputs, target_weights, as provided.
        input_feed = {}
        for l in xrange(self.observation_steps):
            input_feed[self.observation_inputs[l].name] = observation_inputs[l]
        if self.model_type == 'MDN':
            for l in xrange(self.prediction_steps):
                input_feed[self.future_inputs[l].name] = future_inputs[l]
                input_feed[self.target_weights[l].name] = target_weights[l]
        if self.model_type == 'classifier':
                input_feed[self.target_inputs[0].name] = future_inputs[0]
                input_feed[self.target_weights[0].name] = target_weights[0]


        # Output feed: depends on whether we do a backward step or not.
        if train_model:
            output_feed = (self.updates +  # Update Op that does SGD. #This is the learning flag
                         self.gradient_norms +  # Gradient norm.
                         [self.losses] +
                           [self.accuracy])  # Loss for this batch.
        else:
            output_feed = [self.accuracy, self.losses]# Loss for this batch.
            if self.model_type == 'MDN':
                for l in xrange(self.prediction_steps):  # Output logits.
                    output_feed.append(self.MDN_sampled_output[l])
            if self.model_type == 'classifier':
                output_feed.append(self.model_output[0]) # TODO add a softmax here as it is done in the loss funciton.

        outputs = session.run(output_feed, input_feed)
        if summary_writer is not None:

            summary_str = session.run(self.summary_op,input_feed)
            summary_writer.add_summary(summary_str, self.global_step.eval(session=session))
        if train_model:
            return outputs[3], outputs[2], None  # accuracy, loss, no outputs.
        else:
            return outputs[0], outputs[1], outputs[2:]  # accuracy, loss, outputs