from .kernels import fixed_k_mexhat
from .helpers import get_2d_indices
from .match_radial_component import match_radial_component
from .prior import LowRankPrior

from ..opm import calculate_map

import numpy as np
import inspect
import dill as pickle
import os
from sklearn.decomposition import FactorAnalysis


class GaussianProcessOPM():
    """ A Gaussian process used to infer an orientation preference map (OPM) from imaging data.
    """

    def __init__(self, size, prior_rank, prior_method='icd', kernel=fixed_k_mexhat):
        """ Initialize prior fitting method and dimensionalities
        
        Args:
            size: tuple (x, y) or int (results in square map)
            prior_rank: rank of low-rank prior_approximation (only used if prior_method is given)
            prior_method: can be either 'icd' or None
            kernel: kernel function of structure f(x, y, **hyperparams). 
                    defaults to mexican hat with sigma and and alpha as parameters and fixed k.
        """
        self.size = size
        self.idx = get_2d_indices(size)

        self.rank = prior_rank
        self.prior_method = prior_method

        self.kernel = kernel

    def fit_prior(self):
        """ Learn a (low-rank) represenation of the prior covariance.
        
        Return:
            self.prior (fitted LowRankPrior object)
        """
        self.prior = LowRankPrior(self.idx, method=self.prior_method, rank=self.rank)
        self.prior.fit(kernel=self.kernel, **self.kernel_params)
        return self.prior

    def optimize(self, stimuli, responses, verbose=False):
        """ Estimate the prior hyperparameters by matching them to the radial component 
            of the empirical map (see match_radial_components).
            
        Args:
            stimuli: N_cond x N_rep x d array, stimulus conditions for each trial
            responses: N_cond x N_rep x n_x x n_y array, responses from an experiment 
        
        Returns:
            self.kernel_params, dict containing the names and optimized values of the hyperparameters
        """

        # get names and default values for hyperparameters
        s = inspect.signature(self.kernel)
        hyperparams = list(s.parameters.values())[2:]
        p0 = {p.name: p.default for p in hyperparams}

        p_opt = match_radial_component(responses, stimuli, p0=p0)

        self.kernel_params = {p.name: val for p, val in zip(hyperparams, p_opt)}

        if verbose:
            print(self.kernel_params)

        return self.kernel_params

    def fit_posterior(self, stimuli, responses, noise_cov):
        """ Given a set of stimuli and responses, compute the posterior mean and covariance
        
        Args:
            stimuli: N_cond x N_rep x d array, stimulus conditions for each trial
            responses: N_cond x N_rep x n_x x n_y array, responses from an experiment
            noise_cov: n x n array, noise covariance matrix
        
        Returns:
            self.mu_post, self.K_post: posterior mean and covariance
        """

        N = stimuli.shape[0] * stimuli.shape[1]
        d = stimuli.shape[2]

        nx = responses.shape[2]
        ny = responses.shape[3]
        n = nx * ny

        G = self.prior.G
        K = G @ G.T + self.prior.D
        beta = 2 / N

        S = np.linalg.inv(noise_cov)

        # calculate empirical map
        mhat = calculate_map(responses, stimuli).reshape(n * d, 1)

        K_post_c = K - 1 / beta * K @ (S - S @ G @ np.linalg.inv(beta * np.eye(self.rank) + G.T @ S @ G) @ G.T @ S) @ K

        self.K_post = np.kron(np.eye(d), K_post_c)

        # inefficient version (keeping the comment for readability)
        # K_post = np.linalg.inv(np.linalg.inv(K_m) + np.kron(N/2 * np.eye(d), K_e))

        # different way of writing vector averaging (calculate_map, i.e. max likelihood)
        # vr = np.zeros((n*d,1))
        # for v, r in zip(V, R):
        #    vr += np.kron(v, r)[:,np.newaxis]

        # TODO: this can be made more efficient by leveraging the low-rank stuff

        self.mu_post = np.kron(np.eye(d), K_post_c @ S) @ mhat

        return self.mu_post, self.K_post

    def learn_noise_model(self, V, R, mu, **noise_kwargs):
        """ Fit the noise model given the posterior mean

        Args:
            V: stimuli, N_cond x N_rep x d array, stimulus conditions for each trial
            R: responses, N_cond x N_rep x n_x x n_y array, responses from an experiment
            mu: posterior mean
            **noise_kwargs: contains 'method' and 'q'

        Returns:
            sigma, n x n noise covariance matrix
        """
        d = V.shape[2]
        N = R.shape[0] * R.shape[1]
        n = R.shape[2] * R.shape[3]

        # compute residuals
        z = R.reshape(N, n) - V.reshape(N, d) @ mu.reshape(d, n)

        if noise_kwargs['method'] == 'factoran':
            # fit factor analysis model
            fa = FactorAnalysis(n_components=noise_kwargs['q'])
            fa.fit(z)
            sigma = fa.get_covariance()

        elif noise_kwargs['method'] == 'indep':
            # pixel variance across trials
            sigma = np.diag(np.var(z, axis=0))

        return sigma

    def fit(self, stimuli, responses, noise='factoran', noise_kwargs=None, verbose=False):
        """ Complete fitting procedure:
            - Estimate prior hyperparameters using empirical map
            - Fit prior covariance
            - Compute the posterior mean and covariance (assuming a given noise covariance)
        
        Args:
            stimuli: N_cond x N_rep x d array, stimulus conditions for each trial
            responses: N_cond x N_rep x n_x x n_y array, responses from an experiment 
            noise_cov: can be
                        - n x n numpy.ndarray, given noise covariance matrix 
                        - 'factoran': iterative factor analysis noise estimation
                        - 'indep': iterative independent noise estimation
            verbose: boolean, do you want to print progress info?
        
        Returns:
            self.mu_post: posterior mean
        """

        # check if valid noise estimation method is specified
        if not (type(noise) is np.ndarray or (type(noise) is str and noise in ['factoran', 'indep'])):
            raise ValueError("Please specify a valid noise model.")

        # get dimensionalities
        d = stimuli.shape[2]
        N_cond = responses.shape[0]
        N_rep = responses.shape[1]
        N = N_cond * N_rep
        n = responses.shape[2] * responses.shape[3]

        if verbose:
            print('*** Estimating prior hyperparameters ***')

        self.optimize(stimuli, responses, verbose=verbose)

        if verbose:
            print('*** Fitting prior ***')

        self.fit_prior()

        if verbose:
            print('*** Fitting posterior ***')

        if type(noise) is np.ndarray:
            # given noise covariance matrix
            self.fit_posterior(stimuli, responses, noise)


        else:
            if noise_kwargs is None:
                noise_kwargs = {}

            # default noise model parameters
            noise_kwargs.setdefault('iterations', 3)
            noise_kwargs.setdefault('q', 2)
            noise_kwargs.setdefault('method', noise)

            # compute initial estimate, assuming the whole signal is noise
            sigma_noise_init = np.zeros((n, n))
            for R_i in responses.reshape(N_cond, N_rep, -1):
                C_i = np.cov(R_i.T)
                sigma_noise_init += C_i

            sigma_noise_init /= N_cond

            # iterative noise fitting procedure
            for i in range(noise_kwargs['iterations']):

                if verbose:
                    print('Fitting noise model: iteration {}'.format(i + 1))

                if i == 0:
                    # in the first step use the initial estimate
                    mu, _ = self.fit_posterior(stimuli, responses, sigma_noise_init)

                # learn the noise model (either indep or factoran) given current posterior mean
                sigma_noise = self.learn_noise_model(V=stimuli, R=responses, mu=mu, **noise_kwargs)

                # get updated estimate of posterior mean using current estimate of noise covariance
                mu, _ = self.fit_posterior(stimuli, responses, sigma_noise)

            self.noise_cov = sigma_noise

        return self.mu_post

    def save(self, fname):
        """ Save this object to a file

        Args:
            fname: file name
        """
        if not isinstance(fname, str):
            raise ValueError("Parameter fname has to be a string.")

        try:
            if not os.path.isdir(os.path.dirname(fname)):
                os.makedirs(os.path.dirname(fname), exist_ok=True)

            pickle.dump(self, open(fname, "wb"))
        except IOError as io:
            print("IOError while saving class: {}".format(io))

    @staticmethod
    def load(fname):
        """ Load a GP from a file

        Args:
            fname: file name

        Returns:
            a GaussianProcessOPM object
        """
        if not isinstance(fname, str):
            raise ValueError("Parameter fname has to be a string.")

        try:
            gp = pickle.load(open(fname, "rb"))
        except IOError as io:
            print("IOError while saving class: {}".format(io))

        return gp