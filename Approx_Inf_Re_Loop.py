import numpy as np
import sys
import scipy.optimize
import warnings
import timeit
import numba
warnings.filterwarnings("ignore")
np.set_printoptions(threshold=sys.maxsize)
np.set_printoptions(formatter={'all': lambda x: str(int(x))})

# Import Nathan's fake data
N = np.load('Nathan Fake 225/N.npy')
M_est = np.load('Nathan Fake 225/M_est.npy')
M_true = np.load('Nathan Fake 225/M_true.npy')
dist = np.load('Nathan Fake 225/d.npy')
K_val = np.load('Nathan Fake 225/K.npy')

### Want to include filter s.t. regions more than 80km cannot be accessed in one timestep
K_cut = np.where(dist > K_val, 0, 1)

# Specify number of time steps (# of slices - 1), and number of regions
n_tsteps = 3
lim = 225
tol = 1e-4
conv_per = 0.001

# Specify value of Lambda coefficient of penalty terms and initial beta and s guesses
lmbda = 10.
beta = np.zeros(1)
s = np.ones(lim)
sbeta = np.concatenate((s, beta))

# Create N(t) and N(t+1) arrays
N_t = N[0:n_tsteps]
N_tp1 = N_trunc_p1 = N[1:(n_tsteps + 1)]

#Compute the average change in N over time for each slice (instead of specifying factor - some kind of average )
fac_list = []
for t in range(n_tsteps):
    avg = 0
    for i in range(lim):
        avg += np.abs(N_t[t][i] - N_tp1[t][i])
    avg = avg/(lim ** 2) # or lim **2? also, maybe need more movement in test data. Like gathering scores higher or smth
    fac_list.append(avg)


# Define pi as function of XYZ
def pi_func(XYZ):
    pi = np.zeros(lim)
    for i in range(lim):
        num = denom = 0
        for t in range(n_tsteps):
            num += XYZ[Y_shift + t * dy[1] + i]
            denom += (XYZ[Y_shift + t * dy[1] + i] + XYZ[Z_shift + t * dz[1] + i])
        pi[i] = num / denom
    return pi

# Define theta as function of pi, beta, s, dist
def theta_func(pi, sbeta, dist):
    s = sbeta[0:lim]
    theta = np.zeros((lim, lim))
    for i in range(lim):
        theta[i][i] = 1 - pi[i]
        for j in range(lim):
            if j != i:
                theta[i][j] = pi[i] * (sbeta[j] * np.exp(-sbeta[-1] * dist[i][j])) / \
                              ((np.sum(s[np.newaxis, :] * np.exp(-sbeta[-1] * dist), axis=1))[i] - s[i])
    return theta

# Define mu as a function of theta
def mu_func(theta):
    mu = np.sum(N_t[:, np.newaxis] * theta[np.newaxis, :], axis=0)
    return mu

# Define Jacobian for approximate log likelihood (minus at output for maximisation)
@numba.jit(parallel=True, nopython=True, nogil=True, cache=True)
def app_jac(XYZ, log_mu, log_Npi, log_Npi_inv):
    X_dev_sm = np.zeros((n_tsteps, lim))
    Y_dev = np.zeros((n_tsteps, lim))
    Z_dev = np.zeros((n_tsteps, lim))
    X_dev = np.zeros((n_tsteps, lim, lim))
    for t in range(n_tsteps):
        for i in range(lim):
            Y_dev[t][i] = log_Npi[t][i] - np.log(XYZ[Y_shift + t * dy[1] + i]) + \
                          lmbda * (N_t[t][i] - (XYZ[Y_shift + t * dy[1] + i] + XYZ[Z_shift + t * dz[1] + i]))
            Z_dev[t][i] = log_Npi_inv[t][i] - np.log(XYZ[Z_shift + t * dz[1] + i]) + \
                          lmbda * (N_t[t][i] - (XYZ[Y_shift + t * dy[1] + i] + XYZ[Z_shift + t * dz[1] + i]))
            for k in range(lim):
                X_dev_sm[t][i] += XYZ[t * dx[2] * dx[1] + i * dx[2] + k]
            for j in range(lim):
                X_dev[t][i][j] = log_mu[0][i][j] - np.log(XYZ[t * dx[2] * dx[1] + i * dx[2] + j]) + \
                                 lmbda * (N_tp1[t][i] - X_dev_sm[t][i])
    return - np.concatenate((X_dev.flatten(), Y_dev.flatten(), Z_dev.flatten()))

# Define approximate inference log likelihood as a function of XYZ and other args (minus at output for maximisation)
@numba.jit(parallel=True, nopython=True, nogil=True, cache=True)
def app_log_lik(XYZ, log_mu, log_Npi, log_Npi_inv):
    X_sm = np.zeros((n_tsteps, lim))
    Y = np.zeros((n_tsteps, lim))
    Z = np.zeros((n_tsteps, lim))
    X = np.zeros((n_tsteps, lim, lim))
    for t in range(n_tsteps):
        for i in range(lim):
            Y[t][i] = XYZ[Y_shift + t * dy[1] + i]
            Z[t][i] = XYZ[Z_shift + t * dz[1] + i]
            for j in range(lim):
                X[t][i][j] = XYZ[t * dx[2] * dx[1] + i * dx[2] + j]
                X_sm[t][i] += XYZ[t * dx[2] * dx[1] + i * dx[2] + j]
    sm = np.sum(X * log_mu + X - X * np.log(X)) + np.sum(Y * log_Npi + Y - Y * np.log(Y)) + np.sum(
        Z * log_Npi_inv + Z - Z * np.log(Z))
    con = np.sum(np.abs(N_t - (Y + Z)) ** 2 + np.abs(N_tp1 - X_sm) ** 2)
    return -(sm - (lmbda / 2.) * con)

# Define function of s and beta with args XYZ. Note: No Jacobian this time - approx grad used instead. (minus at output for maximisation)
@numba.jit(parallel=True)
def f_s_b(sbeta, XYZ):
    Y = np.zeros((n_tsteps, lim))
    Z = np.zeros((n_tsteps, lim))
    X = np.zeros((n_tsteps, lim, lim))
    for t in range(n_tsteps):
        for i in range(lim):
            Y[t][i] = XYZ[Y_shift + t * dy[1] + i]
            Z[t][i] = XYZ[Z_shift + t * dz[1] + i]
            for j in range(lim):
                X[t][i][j] = XYZ[t * dx[2] * dx[1] + i * dx[2] + j]
    sm = 0
    sm += np.sum(X * np.log(sbeta[0: lim])[np.newaxis, :, np.newaxis])
    sm += - np.sum(
        Y * np.log(np.sum(sbeta[0: lim][np.newaxis, :] * np.exp(-sbeta[-1] * dist), axis=1))[np.newaxis, :])
    sm += - sbeta[-1] * np.sum(dist[np.newaxis, :] * X)
    return - sm

# Define log likelihood for final M optimisation including penalty term (minus at output for maximisation)
@numba.jit(parallel=True, nopython=True, nogil=True, cache=True)
def final_log_lik(M, pi, sbeta, exp_sum, pi_inv):
    M_diag = M_off = M_full = M_trans = np.zeros((n_tsteps, lim, lim))
    for t in range(n_tsteps):
        for i in range(lim):
            for j in range(lim):
                M_full[t][i][j] = M[t * dx[2] * dx[1] + i * dx[2] + j]
                M_trans[t][i][j] = M[t * dx[2] * dx[1] + j * dx[2] + i]
                if i == j:
                    M_diag[t][i][j] = M[t * dx[2] * dx[1] + i * dx[2] + j]
                if i != j:
                    M_off[t][i][j] = M[t * dx[2] * dx[1] + i * dx[2] + j]
    M_diag_i = np.sum(np.sum(M_diag, axis=2), axis=0)
    M_off_i = np.sum(np.sum(M_off, axis=2), axis=0)
    M_off_j = np.sum(np.sum(M_off, axis=1), axis=0)
    M_off_ij = np.sum(M_off, axis=0)
    M_full_ti = np.sum(M_full, axis=2)
    M_trans_ti = np.sum(M_trans, axis=2)
    sm = np.sum(np.log(pi_inv) * M_diag_i) + np.sum(np.log(pi) * M_off_i) + np.sum(np.log(sbeta[0: lim]) * M_off_j) + \
         np.sum(-sbeta[-1] * dist * M_off_ij) + np.sum(-np.log(exp_sum) * M_off_i) + \
         np.sum(M_full - M_full * np.log(M_full)) - \
         lmbda/2. * np.sum(np.abs(N_t - M_full_ti)**2 + np.abs(N_tp1 - M_trans_ti)**2)
    return -sm

# Define Jacobian for final log likelihood including penalty term (minus at output for maximisation)
@numba.jit(parallel=True, nopython=True, nogil=True, cache=True)
def final_jac(M, pi, sbeta, exp_sum, pi_inv):
    M_jac = np.zeros((n_tsteps, lim, lim))
    M_par = np.zeros((n_tsteps, lim))
    M_par2 = np.zeros((n_tsteps, lim))
    for t in range(n_tsteps):
        for i in range(lim):
            for k in range(lim):
                M_par[t][i] += M[t * dx[2] * dx[1] + i * dx[2] + k]
                M_par2[t][i] += M[t * dx[2] * dx[1] + k * dx[2] + i]
    for t in range(n_tsteps):
        for i in range(lim):
            for j in range(lim):
                if j == i:
                    M_jac[t][i][j] = np.log(pi_inv[i]) - M[t * dx[2] * dx[1] + i * dx[2] + j] + lmbda * \
                                     (N_t[t][i] + N_tp1[t][j] - M_par[t][i] - M_par2[t][j])
                if j != i:
                    M_jac[t][i][j] = np.log(pi[i]) + np.log(sbeta[0: lim][j]) - sbeta[-1] * dist[i][j] - \
                                     (np.log(exp_sum))[i] - np.log(M[t * dx[2] * dx[1] + i * dx[2] + j]) + lmbda * \
                                     (N_t[t][i] + N_tp1[t][j] - M_par[t][i] - M_par2[t][j])
    M_jac = M_jac.flatten()
    return - M_jac

# Initialise M outside loop - new M will feed in after first iteration
M = np.zeros((n_tsteps, lim, lim))
for t in range(n_tsteps):
    M[t] = np.random.rand(lim, lim) * K_cut * fac_list[t]
    for i in range(lim):
        M[t][i][i] = N_t[t][i]

rnd = 0
NAE_lim = False
# Start External Loop and start timer
start = timeit.default_timer()
while NAE_lim == False:

    # Initialise XYZ matrices using current M array
    X = np.zeros((n_tsteps, lim, lim))
    Y, Z = (np.zeros((n_tsteps, lim)) for b in range(2))
    for t in range(n_tsteps):
        for i in range(lim):
            for j in range(lim):
                X[t][i][j] += M[t][i][j]
                if j != i:
                    Y[t][i] += M[t][i][j]
                if j == i:
                    Z[t][i] = M[t][i][j]
    XYZ = np.concatenate((X.flatten(), Y.flatten(), Z.flatten()))
    M = M.flatten()

    # Define as global variables shifts to locations of X, Y and Z elements within XYZ
    dx = X.shape
    dy = dz = Y.shape
    Y_shift, Z_shift = n_tsteps * lim ** 2, n_tsteps * lim * (1 + lim)

    current = 0.
    current_2 = 0.
    conv = False

    # Begin inner while here:
    while conv == False:
        existing = current
        existing_2 = current_2

        ### pi update (eqn 10)
        pi = pi_func(XYZ)

        ### Calculate theta matrix (not time dependent) (eqn 2)
        theta = theta_func(pi, sbeta, dist)

        ### Calculate mu given theta (assumes no equal distances)
        mu = mu_func(theta)

        # Define additional arguments with correct dimensionality to feed into approximate log likelihood
        log_mu = np.log(mu[np.newaxis, :]) * np.ones((n_tsteps, lim, lim)) / np.ones((n_tsteps, lim, lim))
        log_Npi = np.log(N_t * pi[np.newaxis, :])
        pi_inv = np.zeros(lim)
        for i in range(lim):
            pi_inv[i] = 1 - pi[i]
        log_Npi_inv = np.log(N_t * pi_inv)

        ### XYZ optimisation
        bnds = []
        for i in range(XYZ.shape[0]):
            bnds.append((1e-9, None))
        newvals = scipy.optimize.fmin_l_bfgs_b(app_log_lik, XYZ, args=(log_mu, log_Npi, log_Npi_inv), fprime=app_jac, bounds=bnds)#, epsilon=nv_eps, factr=nv_factr, pgtol=nv_pgtol)
        XYZ, current = newvals[0], newvals[1]
        print '---------------------------'
        print 'Round ', rnd
        try:
            assert newvals[2]['warnflag'] == 0
        except AssertionError as err:
            print("XYZ error ", newvals[2]['task'])
            print(err)
        print 'Current approximate log likelihood = ', - current
        for i in XYZ:
            if i < 0:
                print "XYZ bounds exceeded"

        ### Maximising f_s_b
        bnds = []
        for i in range(sbeta.shape[0]-1):
            bnds.append((0, None))  ### s and beta bounds
        bnds.append((None, None))

        newvals2 = scipy.optimize.fmin_l_bfgs_b(f_s_b, sbeta, args=(XYZ,), approx_grad=True, bounds=bnds)#, epsilon=beta_eps, factr=beta_factr, pgtol=beta_pgtol)
        sbeta, current_2 = newvals2[0], newvals2[1]
        print 'Current f_s_b function value: ', current_2
        print 'beta = ', sbeta[-1]
        for sval in sbeta[0: lim]:
            if sval < 0:
                print "s bounds exceeded"
        try:
            assert newvals2[2]['warnflag'] == 0
        except AssertionError as err:
            print("beta error ", newvals2[2]['task'])
            print(err)
        if abs((existing - current)/current)*100 < conv_per and abs((existing_2 - current_2)/current_2)*100 < conv_per:
            print "Converged to within ", conv_per, "%"
            conv = True

    ### pi update (eqn 10)
    pi = pi_func(XYZ)

    # Define additional arguments with correct dimensionality to feed into M optimisation
    exp_sum = np.sum((sbeta[0: lim])[np.newaxis, :] * np.exp(-sbeta[-1] * dist), axis=1) - sbeta[0: lim]
    pi_inv = pi.copy()
    for i in range(lim):
        pi_inv[i] = 1 - pi[i]

    # Implement K cutoff for disallowed distances
    K_arr = np.zeros((n_tsteps, lim, lim))
    for t in range(n_tsteps):
        K_arr[t] = K_cut
    K_arr = K_arr.flatten()

    ### Performing final optimisation
    bnds = []
    for i in range(M.shape[0]):
        if K_arr[i] == 0:
            bnds.append((.1, .4))
        else:
            bnds.append((.1, 1e4))
    Mvals = scipy.optimize.minimize(fun=final_log_lik, x0=M, method='L-BFGS-B', jac=final_jac, args=(pi, sbeta, exp_sum, pi_inv), bounds=bnds, options={'ftol': tol, 'eps': 1e-10})
    Mfinal = Mvals.x
    print '---------------------------'
    try:
        assert Mvals.success == 0
    except AssertionError as err:
        print("Mvals error ", Mvals.message)
        print(err)
    print 'Final Log Likelihood value: ', - Mvals.fun

    ## Returning M matrices to original form
    M_mat = np.zeros((n_tsteps, lim, lim))
    for t in range(n_tsteps):
        for i in range(lim):
            for j in range(lim):
                M_mat[t][i][j] = int(Mfinal[t * dx[2] * dx[1] + i * dx[2] + j])

    # Finally calculate NAE
    num = 0
    num_Nathan = 0
    denom = 0
    for i in range(n_tsteps):
        for j in range(lim):
            for k in range(lim):
                num += np.abs(M_true[i][j][k] - M_mat[i][j][k])
                num_Nathan += np.abs(M_true[i][j][k] - M_est[i][j][k])
                denom += M_true[i][j][k]
    NAE_Emily = num/denom
    NAE_Nathan = num_Nathan/denom
    print 'Current NAE = ', NAE_Emily

    # Calculate also the off-diagonal NAE
    num = 0
    num_Nathan = 0
    denom = 0
    for i in range(n_tsteps):
        for j in range(lim):
            for k in range(lim):
                if k != j:
                    num += np.abs(M_true[i][j][k] - M_mat[i][j][k])
                    num_Nathan += np.abs(M_true[i][j][k] - M_est[i][j][k])
                    denom += M_true[i][j][k]
    off_NAE_Emily = num/denom
    off_NAE_Nathan = num_Nathan/denom

    # Update M to new value and specify new iteration number
    M = M_mat
    rnd += 1

    # End while only if suitable NAE obtained
    if NAE_Emily < 0.1:
        NAE_lim = True
        stop = timeit.default_timer()

# Print output
print '---------------------------'
print 'Run time = ', np.round(((stop - start)/60),2), 'mins'
print "NAE Emily = ", NAE_Emily
print "NAE Nathan = ", NAE_Nathan
print "off diagonal NAE Emily = ", off_NAE_Emily
print "off diagonal NAE Nathan = ", off_NAE_Nathan
print 'Number of regions = ', lim
print 'Number of timesteps = ', n_tsteps
print 'beta = ', sbeta[-1]
print 'ftol, Lambda = ', [tol, lmbda]
# print 's = ', sbeta[0: lim]
# print 'M_Emily(t=0) = \n', M_mat[0]
# print 'M_true(t=0) = \n', M_true[0]
# print 'M_Nathan(t=0) = \n', M_est[0]


