import tensorflow as tf
import numpy as np

# Library that implements Alex Graves 2014 paper

def tf_2d_normal(x1, x2, mu1, mu2, s1, s2, rho):
    # eq # 24 and 25 of http://arxiv.org/abs/1308.0850
    norm1 = tf.sub(x1, mu1)
    norm2 = tf.sub(x2, mu2)
    s1s2 = tf.mul(s1, s2)
    z = tf.square(tf.div(norm1, s1)) + tf.square(tf.div(norm2, s2)) - 2 * tf.div(tf.mul(rho, tf.mul(norm1, norm2)),
                                                                                 s1s2)
    negRho = 1 - tf.square(rho)
    result = tf.exp(tf.div(-z, 2 * negRho))
    denom = 2 * np.pi * tf.mul(s1s2, tf.sqrt(negRho))
    result = tf.div(result, denom)
    return result

def get_lossfunc(z_pi, z_mu1, z_mu2, z_sigma1, z_sigma2, z_corr, x1_data, x2_data):
    result0 = tf_2d_normal(x1_data, x2_data, z_mu1, z_mu2, z_sigma1, z_sigma2, z_corr)
    # implementing eq # 26 of http://arxiv.org/abs/1308.0850
    result1 = tf.mul(result0, z_pi)
    result1 = tf.reduce_sum(result1, 1, keep_dims=True)
    result = -tf.log(tf.maximum(result1, 1e-20))  # at the beginning, some errors are exactly zero.

    return tf.reduce_sum(result)


def lossfunc_wrapper(prediction, ground_truth):
    z_pi, z_mu1, z_mu2, z_sigma1, z_sigma2, z_corr = get_mixture_coef(prediction)
    #HACK to force NaN's so I can write a catcher
    #z_pi, z_mu1, z_mu2, z_sigma1, z_sigma2, z_corr = tf.split(1,6,prediction)
    x1_data, x2_data = tf.split(1,2,ground_truth)
    return get_lossfunc(z_pi, z_mu1, z_mu2, z_sigma1, z_sigma2, z_corr, x1_data, x2_data)


# below is where we need to do MDN splitting of distribution params
def get_mixture_coef(output):
    # returns the tf slices containing mdn dist params
    # ie, eq 18 -> 23 of http://arxiv.org/abs/1308.0850
    z = output
    z_pi, z_mu1, z_mu2, z_sigma1, z_sigma2, z_corr = tf.split(1,6,z)

    # process output z's into MDN paramters
    # softmax all the pi's:
    max_pi = tf.reduce_max(z_pi, 1, keep_dims=True)
    z_pi = tf.sub(z_pi, max_pi)
    z_pi = tf.exp(z_pi)
    normalize_pi = tf.inv(tf.reduce_sum(z_pi, 1, keep_dims=True))
    z_pi = tf.mul(normalize_pi, z_pi)

    # exponentiate the sigmas and also make corr between -1 and 1.
    z_sigma1 = tf.exp(z_sigma1)
    z_sigma2 = tf.exp(z_sigma2)
    # Bound the correlation coefficient to within 1,-1
    z_corr = tf.minimum(0.999,tf.maximum(-0.999,tf.tanh(z_corr)))


    #tf.histogram_summary("z_pi", z_pi)
    #tf.histogram_summary("z_mu1", z_mu1)
    #tf.histogram_summary("z_mu2", z_mu2)
    #tf.histogram_summary("z_sigma1",z_sigma1)
    #tf.histogram_summary("z_sigma2",z_sigma2)
    #tf.histogram_summary("z_corr",z_corr)

    return [z_pi, z_mu1, z_mu2, z_sigma1, z_sigma2, z_corr]


def sample(output):
    o_pi, o_mu1, o_mu2, o_sigma1, o_sigma2, o_corr = get_mixture_coef(output)
    # Take in output params
    # return a single sample used for squence genereation / loopback

    #I have to replace these functions with tf ones.
    #Replace this with tf.multinomial
    def get_pi_idx(x, pdf):
        N = pdf.size
        accumulate = 0
        for i in range(0, N):
            accumulate += pdf[i]
            if (accumulate >= x):
                return i
        print 'error with sampling ensemble'
        return -1

    #There is a random_normal
    def sample_gaussian_2d(mu1, mu2, s1, s2, rho):
        # mean = [mu1, mu2]
        #cov = [[s1 * s1, rho * s1 * s2], [rho * s1 * s2, s2 * s2]]
        #THIS CREATES A 2x2x11
        # I need a 11x2x2

        #TODO THURSDAY
        covUL = tf.expand_dims(tf.square(s1),1)
        covUR = tf.expand_dims(tf.mul(rho,tf.mul(s1,s2)),1)
        covLL = tf.expand_dims(tf.mul(rho,tf.mul(s1,s2)),1)
        covLR = tf.expand_dims(tf.square(s2),1)

        #WRONG this makes size 22, I want 11,2
        covU = tf.expand_dims(tf.concat(1,[covUL,covUR]),2)
        covL = tf.expand_dims(tf.concat(1,[covLL,covLR]),2)
        cov = tf.concat(2,[covU,covL])

        # #tf.random_normal? its not multivariate, but it will have to do.
        # #tf.self_adjoint_eigvals can be used on the cov matrix
        #
        # x = np.random.multivariate_normal(mean, cov, 1)
        # return x[0][0], x[0][1]

        #See https://en.wikipedia.org/wiki/Multivariate_normal_distribution#Drawing_values_from_the_distribution
        #step 2
        #TODO DEBUG
        batch_size = tf.shape(mu1)
        #batch_size = mu1.get_shape()
        convar = tf.constant([2])
        random_shape = tf.concat(0,[convar,batch_size])
        #TODO batch?
        z = tf.expand_dims(tf.transpose(tf.random_normal(random_shape)),2)
        #/TODO
        L = tf.batch_cholesky(cov)
        mean = tf.concat(1,[tf.expand_dims(mu1,1),
                            tf.expand_dims(mu2,1)])
        Lz = tf.squeeze(tf.batch_matmul(L,z),[2])
        x = tf.add(mean,Lz)


        return x

    #idx = get_pi_idx(random.random(), o_pi[0]) #o_pi is shape (11,29) I need 11 results, one for each dist of 29 params.
    idx = tf.to_int32(tf.multinomial(tf.log(o_pi),1))

    #TODO - gather_nd does not have a gradient function. Replace with:
    # 1 - convert batch_idx to 1 hot vector
    # 2 - multiply
    # 3 - reduce-sum
    #Because the documentation for gather_nd is easier to read than tf.gather
    batch_range = tf.expand_dims(tf.range(0,idx.get_shape()[0]),1)
    batch_idx = tf.concat(1,[batch_range,idx])
    next = sample_gaussian_2d(tf.gather_nd(o_mu1,batch_idx),
                              tf.gather_nd(o_mu2,batch_idx),
                              tf.gather_nd(o_sigma1,batch_idx),
                              tf.gather_nd(o_sigma2,batch_idx),
                              tf.gather_nd(o_corr,batch_idx))

    return next