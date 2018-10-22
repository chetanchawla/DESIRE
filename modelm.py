'''
DESIRE: Deep Stochastic IOC RNN Encoder-decoder for Distant Future
Prediction in Dynamic Scenes with Multiple Interacting Agents

Author: Todor Davchev
Date: 13th February 2017
'''

import copy
import random
import time
import sys
import os

# from grid import getSequenceGridMask
import ipdb
import numpy as np
import prettytensor as pt
import tensorflow as tf
from tensorflow.python.ops import rnn, rnn_cell#, seq2seq
import tensorflow.contrib.seq2seq as seq2seq
from tensorflow.python.framework import dtypes

sys.path.append("/home/todor/Documents/workspace/DESIRE")
exec(open("utils/convolutional_vae_util.py").read())
# from convolutional_vae_util import deconv2d


class DESIREModel(object):
    '''
    DESIRE model. Represents a Stochastic Inverse Reinforcement Learning
    Encoder-Decoder for Distant Future Prediction in Dynamic Scenes with
    Multiple Interacting Agents
    '''

    def __init__(self, args):
        self.args = args
        # TODO: remove the unnecesary variables
        # TODO: rename decoder_output to hidden_features
        # input depth = sequence
        # input_height = max number of people
        # input width = id,x,y
        self.filter_height = 1
        self.filter_width = self.args.seq_length
        self.in_channels = 2 # one for x and y for each trajectory point
        self.channel_multiplier = 100 # this is the feature map
        # strides[0] = strides[3] for same horizontal and vertical strides
        self.strides = [1, self.args.stride, self.args.stride, 1]
        self.input_size = 3
        self.encoder_output = self.args.e_dim
        self.decoder_output = self.args.d_dim # hidden_features
        self.rnn_size = self.args.rnn_size # hidden_features
        self.seq_length = self.args.seq_length # time_steps
        self.num_layers = self.args.num_layers
        self.batch_size = self.args.batch_size
        self.latent_size = self.args.latent_size
        self.input_shape = [int(np.sqrt(2*self.rnn_size)),
                            int(np.sqrt(2*self.rnn_size))]
        self.vae_input_size = np.prod(self.input_shape)
        self.max_num_obj = self.args.max_num_obj

        self.output_states = None
        self.input_data = None
        self.target_data_enc = None
        self.target_data = None
        self.optimizer = None
        self.accuracy = None
        self.acc_summary = None
        self.learning_rate = None
        self.output_size = None
        self.gru_states = None
        self.output_states = None
        self.spatial_input = None
        self.enc_state_x = None
        self.enc_state_y = None

        self.build_model()

    def build_model(self):
        '''
        Building the DESIRE Model
        '''
        # TODO: fix input size to be of size MNOx3 and convolve over the MNOx2D matrix
        # TODO: fix temporal_data to be of seqxMNOxinput sizeinstead
        # shape=[1, self.args.max_num_obj, self.args.seq_length, 2],
        self.temporal_data = tf.placeholder(
            tf.float32,
            shape=[1, self.args.max_num_obj, self.args.seq_length, self.input_size],
            name="temporal_data"
        )
        self.input_data = tf.placeholder(
            tf.float32,
            shape=[self.args.max_num_obj, self.args.seq_length, self.input_size],
            name="input_data"
        )
        self.target_data_enc = tf.placeholder(
            tf.float32,
            shape=[self.args.max_num_obj, self.seq_length, self.input_size],
            name="target_data_enc"
        )
        self.target_data = tf.placeholder(
            tf.float32,
            shape=[self.args.max_num_obj, self.seq_length, self.input_size],
            name="target_data"
        )
        self.learning_rate = tf.Variable(
            self.args.learning_rate,
            trainable=False,
            name="learning_rate"
        )

        self.output_size = self.seq_length * 2

        weights, biases = self.define_weights()

        temporal_shape = self.temporal_data.get_shape().as_list()
        temporal_shape[3] = temporal_shape[3] - 1
        temporal_input = tf.slice(
            self.temporal_data,
            [0, 0, 0, 0],
            temporal_shape
        )
        temporal_ids = self.temporal_data[:, :, :, 2:]
        # The Formula for the Model
        # Temporal convolution
        with tf.variable_scope("temporal_convolution"):
            self.rho_i = tf.nn.relu(tf.add( \
                tf.nn.depthwise_conv2d(
                    temporal_input, weights["temporal_w"],
                    self.strides,
                    padding='VALID'
                ),
                biases["temporal_b"]))

        # Encoder
        with tf.variable_scope("gru_cell"):
            gru_cell = tf.nn.rnn_cell.GRUCell(self.decoder_output)
            cells = tf.nn.rnn_cell.MultiRNNCell(
                [gru_cell]*self.num_layers,
                state_is_tuple=False
            )

        with tf.variable_scope("gru_y_cell"):
            gru_cell_y = tf.nn.rnn_cell.GRUCell(self.decoder_output)
            cells_y = tf.nn.rnn_cell.MultiRNNCell(
                [gru_cell_y]*self.num_layers,
                state_is_tuple=False
            )

        # Define GRU states for each pedestrian
        with tf.variable_scope("gru_states"):
            self.gru_states = tf.zeros(
                [self.args.max_num_obj, cells.state_size],
                name="gru_states"
            )
            self.enc_state_x = tf.split(
                0, self.args.max_num_obj, self.gru_states
            )

        with tf.variable_scope("gru_states_y"):
            self.gru_states_y = tf.zeros(
                [self.args.max_num_obj, cells_y.state_size],
                name="gru_states_y"
            )
            self.enc_state_y = tf.split(
                0, self.args.max_num_obj, self.gru_states_y
            )

        with tf.variable_scope("feature_pooling"):
            self.f_pool = \
                tf.zeros([self.args.max_num_obj, 7, self.seq_length, 2*self.channel_multiplier])
            self.feature_pooling = \
                tf.split(0, self.args.max_num_obj, self.f_pool)
            self.feature_pooling = [tf.squeeze(_input, [0]) for _input in self.feature_pooling]

        # Define hidden output states for each pedestrian
        with tf.variable_scope("output_states"):
            self.output_states = \
                tf.split(0, self.args.max_num_obj, \
                    tf.zeros([self.args.max_num_obj, 7, cells.output_size]))

        # List of tensors each of shape args.maxNumPedsx3 corresponding to
        # each frame in the sequence
        with tf.name_scope("frame_data_tensors"):
            frame_data = [tf.squeeze(input_, [0]) \
                for input_ in tf.split(0, self.args.max_num_obj, self.input_data)]

        with tf.name_scope("frame_target_data_tensors"):
            frame_target_data = [tf.squeeze(target_, [0]) \
                for target_ in tf.split(0, self.args.max_num_obj, self.target_data)]

        # Cost
        with tf.name_scope("Cost_related_stuff"):
            self.cost = tf.constant(0.0, name="cost")
            self.counter = tf.constant(0.0, name="counter")
            self.increment = tf.constant(1.0, name="increment")

        # Containers to store output distribution parameters
        with tf.name_scope("Distribution_parameters_stuff"):
            self.initial_output = \
                tf.split(0, self.args.max_num_obj, \
                    tf.zeros([self.args.max_num_obj, self.output_size]))

        # Tensor to represent non-existent ped
        with tf.name_scope("Non_existent_obj_stuff"):
            nonexistent_obj = tf.constant(0.0, name="zero_obj")

        # for seq, frame in enumerate(frame_data):
        #     current_frame_data = frame  # MNP x 3 tensor
        #     current_target_frame_data = frame_target_data[seq] # MNP x 3 tensor
        for obj in xrange(0, self.args.max_num_obj):
            # does this assume that every next sequence will depend on the previous ?
            # not sure since it will only produce K and then choose 1 out of them
            obj_id = frame_data[obj][0][0]
            with tf.name_scope("extract_input_obj"):
                spatial_input_x = tf.split(
                    0, self.seq_length,
                    tf.squeeze(tf.slice(
                        frame_data,
                        [obj, 0, 1],
                        [1, self.seq_length, 2]
                    ), [0])
                )
                spatial_input_y = tf.split(
                    0, self.seq_length,
                    tf.squeeze(tf.slice(
                        frame_target_data,
                        [obj, 0, 1],
                        [1, self.seq_length, 2]
                    ), [0])
                )

            with tf.variable_scope("encoding_operations_x", \
                        reuse=True if obj > 0 else None):
                _, self.enc_state_x[obj] = \
                    rnn.rnn(cells, spatial_input_x, dtype=dtypes.float32)

            with tf.variable_scope("encoding_operations_y", \
                        reuse=True if obj > 0 else None):
                _, self.enc_state_y[obj] = \
                    rnn.rnn(cells_y, spatial_input_y, dtype=dtypes.float32)

            with tf.name_scope("concatenate_embeddings"):
                # Concatenate the summaries c1 and c2
                complete_input = tf.concat(1, [self.enc_state_x[obj], self.enc_state_y[obj]])

            # fc layer
            with tf.variable_scope("fc_c"):
                vae_inputs = tf.nn.relu( \
                    tf.nn.xw_plus_b( \
                        complete_input, weights["w_hidden_enc1"], biases["b_hidden_enc1"]))

            # Convolutional VAE
            # z = mu + sigma * epsilon
            # epsilon is a sample from a N(0, 1) distribution
            # Encode our data into z and return the mean and covariance
            with tf.variable_scope("zval", reuse=True if obj > 0 else None):
                z_mean, z_log_sigma_sq = \
                    self.vae_encoder(vae_inputs, self.latent_size)
                eps_batch = z_log_sigma_sq.get_shape().as_list()[0] \
                    if z_log_sigma_sq.get_shape().as_list()[0] is not None else self.batch_size
                eps = tf.random_normal(
                    [eps_batch, self.latent_size], 0.0, 1.0, dtype=tf.float32)
                zval = tf.add(z_mean, tf.mul(tf.sqrt(tf.exp(z_log_sigma_sq)), eps))
                # Get the reconstructed mean from the decoder
                x_reconstr_mean = \
                    self.vae_decoder(zval, self.vae_input_size)
                # z_summary = tf.summary.histogram("zval", zval)

            # fc layer
            with tf.variable_scope("fc_softmax"):
                multipl = tf.add(
                    tf.matmul(x_reconstr_mean, weights["w_post_vae"]),
                    biases["b_post_vae"])
                multipl = tf.nn.relu(multipl)
                multipl = tf.nn.softmax(multipl)

            # Decoder 1
            with tf.variable_scope("hidden_states", reuse=True if obj > 0 else None):
                hidden_state_x = [tf.mul(multipl, self.enc_state_x[obj]) for i in xrange(7)]
                self.output_states[obj], self.enc_state_x[obj] = \
                    seq2seq.rnn_decoder(
                        hidden_state_x,
                        self.enc_state_x[obj],
                        cells)
                self.output_states[obj] = [
                    tf.split(
                        0, self.seq_length, tf.squeeze(_item, [0])
                    ) for _item in self.output_states[obj]]

            rho_i = tf.squeeze(self.rho_i, [0])
            rho_i = tf.squeeze(rho_i, [1])
            pooling_list = []
            for prediction_k in xrange(len(self.output_states[obj])):
                pooling_list.append([])
                for step_t in xrange(len(self.output_states[obj][prediction_k])):
                    pooling_list[prediction_k].append(
                        tf.concat(
                            0, [tf.multiply
                                (
                                    self.output_states[obj][prediction_k][step_t][0],
                                    rho_i[obj][:100]
                                ),
                                tf.multiply
                                (
                                    self.output_states[obj][prediction_k][step_t][1],
                                    rho_i[obj][100:]
                                )]
                            ))

            self.feature_pooling[obj] = tf.pack(pooling_list)
            # FROM HERE ON WE NEED TO IMPROVE !!!
            # RANKING AND REFINING SHOULD GO BEFORE WHAT FOLLOWS HERE !!!

            # # Apply the linear layer. Output would be a tensor of shape 1 x output_size
            # with tf.name_scope("output_linear_layer"):
            #     self.initial_output[obj] = \
            #         tf.nn.xw_plus_b(
            #             self.output_states[obj],
            #             weights["output_w"],
            #             biases["output_b"])

            # with tf.name_scope("extract_target_obj"):
            #     # Extract x and y coordinates of the target data
            #     # x_data and y_data would be tensors of shape 1 x 1
            #     [x_data, y_data] = \
            #         tf.split(1, 2, tf.slice(current_target_frame_data, [obj, 1], [1, 2]))
            #     target_obj_id = current_target_frame_data[obj, 0]

            # with tf.name_scope("get_coef"):
            #     # Extract coef from output of the linear output layer
            #     [o_mux, o_muy, o_sx, o_sy, o_corr] = self.get_coef(self.initial_output[obj])

            # TODO: check if KLD loss actually has reconstruction loss in it
            # TODO: make sure that the CVAE implementation is truly from the same paper
            # TODO: Figure out how/if necessary to divide by K the reconstr_loss
            # TODO: The reconstr loss does not sample from a distribution anymore but instead
            #       chooses the most probable trajectory from the IOC (verify)
            with tf.name_scope("calculate_loss"):
                # Calculate loss for the current ped
                reconstr_loss = \
                    self.get_reconstr_loss(o_mux, o_muy, o_sx, o_sy, o_corr, x_data, y_data)
                kld_loss = self.kld_loss(
                    vae_inputs,
                    x_reconstr_mean,
                    z_log_sigma_sq,
                    z_mean
                )
                loss = tf.reduce_mean(reconstr_loss+kld_loss)

            with tf.name_scope("increment_cost"):
                # If it is a non-existent object, it should not contribute to cost
                # If the object doesn't exist in the next frame, he/she/it should not
                # contribute to cost as well
                self.cost = tf.select( \
                    tf.logical_or( \
                        tf.equal(obj_id, nonexistent_obj), \
                        tf.equal(target_obj_id, nonexistent_obj)), \
                    self.cost, \
                    tf.add(self.cost, loss))
                self.counter = tf.select( \
                    tf.logical_or( \
                        tf.equal(obj_id, nonexistent_obj), \
                        tf.equal(target_obj_id, nonexistent_obj)), \
                    self.counter, \
                    tf.add(self.counter, self.increment))

        for sequence in xrange(7):
            [o_mux, o_muy, o_sx, o_sy, o_corr] = np.split(self.initial_output[0], 5, 0)
            mux, muy, sx_val, sy_val, corr = \
                    o_mux[0], o_muy[0], np.exp(o_sx[0]), np.exp(o_sy[0]), np.tanh(o_corr[0])
            next_x, next_y = self.sample_gaussian_2d(mux, muy, sx_val, sy_val, corr)

        with tf.name_scope("mean_cost"):
            # Mean of the cost
            self.cost = tf.div(self.cost, self.counter)

        # Get all trainable variables
        tvars = tf.trainable_variables()

        # Get the final LSTM states
        self.final_states = tf.concat(0, self.enc_state_x)

        # Get the final distribution parameters
        self.final_output = self.initial_output

        # Compute gradients
        self.gradients = tf.gradients(self.cost, tvars)

        # Clip the gradients
        grads, _ = tf.clip_by_global_norm(self.gradients, self.args.grad_clip)

        # Define the optimizer
        optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate).minimize(loss)

        # self.loss_summary = tf.scalar_summary("loss", loss)
        # self.cost_summary = tf.scalar_summary("cost", self.cost)
        # self.summaries = tf.merge_all_summaries()
        # self.summary_writer = tf.train.SummaryWriter( \
        #     "logs/" + self.get_name() + self.get_formatted_datetime(), sess.graph)

        # The train operator
        # train_op = optimizer.apply_gradients(zip(grads, tvars))

    def get_name(self):
        '''formated name'''
        return "cvae_input_%dx%d_latent%d_edim%d_ddim%d" % (self.input_shape[0],
                                                            self.input_shape[
                                                                1],
                                                            self.latent_size,
                                                            self.args.e_dim,
                                                            self.args.d_dim)

    def get_formatted_datetime(self):
        '''formated datetime'''
        return str(datetime.datetime.now()).replace(" ", "_") \
                                            .replace("-", "_") \
                                            .replace(":", "_")

    def define_weights(self):
        ''' Define Model's weights'''
        # Weights adn Biases for hidden layer and output layer
        # TODO:Make sure you learn the dimensionalities!!!!!
        weights, biases = {}, {}
        with tf.variable_scope("temporal_weights"):
            # This is the filter window
            weights["temporal_w"] = tf.Variable(tf.truncated_normal( \
                [self.filter_height, self.filter_width, self.in_channels, self.channel_multiplier],
                stddev=0.1))
            biases["temporal_b"] = tf.Variable(tf.random_normal( \
                [self.in_channels*self.channel_multiplier]))

        with tf.variable_scope("hidden_enc_weights"):
            weights["w_hidden_enc1"] = tf.Variable(tf.random_normal( \
                [2*self.decoder_output, self.vae_input_size]))
            biases["b_hidden_enc1"] = tf.Variable(tf.random_normal( \
                [self.vae_input_size]))

        with tf.variable_scope("post_vae_weights"):
            weights["w_post_vae"] = tf.Variable(tf.random_normal( \
                [self.vae_input_size, self.decoder_output]))
            biases["b_post_vae"] = tf.Variable(tf.random_normal( \
                [self.decoder_output]))

        # with tf.variable_scope("output_weights"):
        #     weights["output_w"] = tf.Variable(tf.random_normal( \
        #         [self.rnn_size, self.output_size]))
        #     biases["output_b"] = tf.Variable(tf.random_normal( \
        #         [self.output_size]))

        return weights, biases

    def vae_decoder(self, zval, projection_size, activ=tf.nn.elu, phase=pt.Phase.train):
        '''
        C-VAE Decoder from https://github.com/jramapuram/CVAE/blob/master/cvae.py
        '''
        with pt.defaults_scope(activation_fn=activ,
                               batch_normalize=True,
                               learned_moments_update_rate=0.0003,
                               variance_epsilon=0.001,
                               scale_after_normalization=True,
                               phase=phase):
            return (pt.wrap(zval).
                    reshape([-1, 1, 1, self.latent_size]).
                    deconv2d(4, 128, edges='VALID', phase=phase).
                    deconv2d(5, 64, edges='VALID', phase=phase).
                    deconv2d(5, 32, stride=2, phase=phase).
                    deconv2d(5, 1, stride=2, activation_fn=tf.nn.sigmoid, phase=phase).
                    flatten()).tensor

    def vae_encoder(self, inputs, latent_size, activ=tf.nn.elu, phase=pt.Phase.train):
        '''
        C-VAE Encoder from https://github.com/jramapuram/CVAE/blob/master/cvae.py
        Accepts a cube as inputs and performs 2D convolutions on it
        '''
        with pt.defaults_scope(activation_fn=activ,
                               batch_normalize=True,
                               learned_moments_update_rate=0.0003,
                               variance_epsilon=0.001,
                               scale_after_normalization=True,
                               phase=phase):
            params = (pt.wrap(inputs).
                      reshape([-1, self.input_shape[0], self.input_shape[1], 1]).
                      conv2d(5, 32, stride=2).
                      conv2d(5, 64, stride=2).
                      conv2d(5, 128, edges='VALID').
                      flatten().
                      fully_connected(latent_size * 2, activation_fn=None)).tensor

        mean = params[:, :latent_size]
        stddev = params[:, latent_size:]
        return [mean, stddev]

    def tf_2d_normal(self, x_val, y_val, mux, muy, sx_val, sy_val, rho):
        '''
        Function that implements the PDF of a 2D normal distribution
        params:
        x : input x points
        y : input y points
        mux : mean of the distribution in x
        muy : mean of the distribution in y
        sx : std dev of the distribution in x
        sy : std dev of the distribution in y
        rho : Correlation factor of the distribution
        '''
        # eq 3 in the paper
        # and eq 24 & 25 in Graves (2013)
        # Calculate (x - mux) and (y-muy)
        normx = tf.sub(x_val, mux)
        normy = tf.sub(y_val, muy)
        # Calculate sx_val*sy_val
        sxsy = tf.mul(sx_val, sy_val)
        # Calculate the exponential factor
        z_val = tf.square(tf.div(normx, sx_val)) + tf.square(tf.div(normy, sy_val)) \
            - 2*tf.div(tf.mul(rho, tf.mul(normx, normy)), sxsy)
        neg_rho = 1 - tf.square(rho)
        # Numerator
        result = tf.exp(tf.div(-z_val, 2*neg_rho))
        # Normalization constant
        denom = 2 * np.pi * tf.mul(sxsy, tf.sqrt(neg_rho))
        # Final PDF calculation
        result = tf.div(result, denom)
        return result

    def get_reconstr_loss(self, z_mux, z_muy, z_sx, z_sy, z_corr, x_data, y_data):
        '''
        Function to calculate given a 2D distribution over x and y, and target data
        of observed x and y points
        params:
        z_mux : mean of the distribution in x
        z_muy : mean of the distribution in y
        z_sx : std dev of the distribution in x
        z_sy : std dev of the distribution in y
        z_rho : Correlation factor of the distribution
        x_data : target x points
        y_data : target y points
        '''
        # step = tf.constant(1e-3, dtype=tf.float32, shape=(1, 1))

        # Calculate the PDF of the data w.r.t to the distribution
        result0 = self.tf_2d_normal(x_data, y_data, z_mux, z_muy, z_sx, z_sy, z_corr)

        # For numerical stability purposes
        epsilon = 1e-20

        # Apply the log operation
        result1 = -tf.log(tf.maximum(result0, epsilon))  # Numerical stability

        # Sum up all log probabilities for each data point
        return tf.reduce_sum(result1)

    def get_coef(self, output):
        '''eq 20 -> 22 of Graves (2013)'''

        z_val = output
        # Split the output into 5 parts corresponding to means, std devs and corr
        z_mux, z_muy, z_sx, z_sy, z_corr = tf.split(1, 5, z_val)

        # The output must be exponentiated for the std devs
        z_sx = tf.exp(z_sx)
        z_sy = tf.exp(z_sy)
        # Tanh applied to keep it in the range [-1, 1]
        z_corr = tf.tanh(z_corr)

        return [z_mux, z_muy, z_sx, z_sy, z_corr]

    def kld_loss(self, inputs, x_reconstr_mean, z_log_sigma_sq, z_mean):
        '''Taken from https://jmetzen.github.io/2015-11-27/vae.html'''
        # The loss is composed of two terms:
        # 1.) The reconstruction loss (the negative log probability
        #     of the input under the reconstructed Bernoulli distribution
        #     induced by the decoder in the data space).
        #     This can be interpreted as the number of "nats" required
        #     for reconstructing the input when the activation in latent
        #     is given.
        # reconstr_loss = \
        #     -tf.reduce_sum(inputs * tf.log(tf.clip_by_value(x_reconstr_mean, 1e-10, 1.0))
        #                    + (1.0 - inputs) * tf.log(tf.clip_by_value(1.0 -
        #                                                             x_reconstr_mean, 1e-10, 1.0)),
        #                    1)
        # 2.) The latent loss, which is defined as the Kullback Libeler divergence
        # between the distribution in latent space induced by the encoder on
        #     the data and some prior. This acts as a kind of regularize.
        #     This can be interpreted as the number of "nats" required
        #     for transmitting the the latent space distribution given
        #     the prior.
        latent_loss = -0.5 * tf.reduce_sum(1.0 + z_log_sigma_sq \
                                                - tf.square(z_mean) \
                                                - tf.exp(z_log_sigma_sq), 1)
        # kld_loss = tf.reduce_mean(reconstr_loss + latent_loss)   # average over batch
        kld_loss = tf.reduce_mean(latent_loss)   # average over batch

        return kld_loss

    def sample_gaussian_2d(self, mux, muy, sx_val, sy_val, rho):
        '''
        Function to sample a point from a given 2D normal distribution
        params:
        mux : mean of the distribution in x
        muy : mean of the distribution in y
        sx : std dev of the distribution in x
        sy : std dev of the distribution in y
        rho : Correlation factor of the distribution
        '''
        # Extract mean
        mean = [mux, muy]
        # Extract covariance matrix
        cov = [[sx_val*sx_val, rho*sx_val*sy_val], [rho*sx_val*sy_val, sy_val*sy_val]]
        # Sample a point from the multivariate normal distribution
        x_val = np.random.multivariate_normal(mean, cov, 1)
        return x_val[0][0], x_val[0][1]

    def sample(self, sess, traj, grid, dimensions, true_traj, num=10):
        '''
        Sampling method
        traj is a sequence of frames (of length obs_length)
        so traj shape is (obs_length x maxNumPeds x 3)
        grid is a tensor of shape obs_length x maxNumPeds x maxNumPeds x (gs**2)
        states = sess.run(self.gru_states)
         '''
        # print "Fitting"
        # For each frame in the sequence
        for index, frame in enumerate(traj[:-1]):
            data = np.reshape(frame, (1, self.args.max_num_obj, 3))
            target_data = np.reshape(traj[index+1], (1, self.args.max_num_obj, 3))

            feed = {
                self.input_data: data,
                self.gru_states: states,
                self.target_data: target_data
            }
            [states, cost] = sess.run([self.final_states, self.cost], feed)
            # print cost

        ret = traj

        last_frame = traj[-1]

        prev_data = np.reshape(last_frame, (1, self.args.max_num_obj, 3))

        prev_target_data = np.reshape(true_traj[traj.shape[0]], (1, self.args.max_num_obj, 3))
        # Prediction
        for t_step in range(num):
            print("**** NEW PREDICTION TIME STEP", t_step, "****")
            sys.stdout.flush()
            feed = {
                self.input_data: prev_data,
                self.gru_states: states,
                self.target_data: prev_target_data
            }
            [output, states, cost] = sess.run(
                [self.final_output, self.final_states, self.cost], feed)
            print("Cost", cost)
            sys.stdout.flush()
            # Output is a list of lists where the inner lists contain matrices of shape 1x5.
            # The outer list contains only one element (since seq_length=1) and the inner list
            # contains maxNumPeds elements
            # output = output[0]
            newpos = np.zeros((1, self.args.max_num_obj, 3))
            for objindex, objoutput in enumerate(output):
                [o_mux, o_muy, o_sx, o_sy, o_corr] = np.split(objoutput[0], 5, 0)
                mux, muy, sx_val, sy_val, corr = \
                    o_mux[0], o_muy[0], np.exp(o_sx[0]), np.exp(o_sy[0]), np.tanh(o_corr[0])

                next_x, next_y = self.sample_gaussian_2d(mux, muy, sx_val, sy_val, corr)
                if next_x > 1.0:
                    next_x = 1.0
                if next_y > 1.0:
                    next_y = 1.0

                if prev_data[0, objindex, 0] != 0:
                    print("Pedestrian ID", prev_data[0, objindex, 0])
                    print("Predicted parameters", mux, muy, sx_val, sy_val, corr)
                    print("New Position", next_x, next_y)
                    print("Target Position", prev_target_data[0, objindex, 1], \
                        prev_target_data[0, objindex, 2])
                    print()
                    sys.stdout.flush()

                newpos[0, objindex, :] = [prev_data[0, objindex, 0], next_x, next_y]
            ret = np.vstack((ret, newpos))
            prev_data = newpos
            if t_step != num - 1:
                prev_target_data = \
                    np.reshape(true_traj[traj.shape[0] + t_step + 1], (1, self.args.max_num_obj, 3))

        # The returned ret is of shape (obs_length+pred_length) x maxNumPeds x 3
        return ret
