import os, sys
import pickle
import numpy as np
import tensorflow as tf

from provabgs import infer as Infer

from speculator import SpectrumPCA
from speculator import Speculator

# -------------------------------------------------------
# params
# -------------------------------------------------------
version = "0.1"
model = sys.argv[1]
nbatch = int(sys.argv[2])
i_wave = int(sys.argv[3])
n_pcas = int(sys.argv[4])
Nlayer = int(sys.argv[5])
Nunits = int(sys.argv[6])
b_size = int(sys.argv[7])
desc = "nbatch%i" % b_size
# -------------------------------------------------------
assert os.environ["machine"] == "tiger"

# dat_dir='/scratch/gpfs/chhahn/provabgs/' # hardcoded to tiger directory
dat_dir = "/tigress/chhahn/provabgs/emulator/"
wave = np.load(os.path.join(dat_dir, "wave.%s.npy" % model))

# wavelength bins
wave_bin = [
    (wave >= 1000) & (wave < 2000),
    (wave >= 2000) & (wave < 3600),
    (wave >= 3600) & (wave < 5500),
    (wave >= 5500) & (wave < 7410),
    (wave >= 7410) & (wave < 60000),
][i_wave]

str_wbin = [".w1000_2000", ".w2000_3600", ".w3600_5500", ".w5500_7410", ".w7410_60000"][
    i_wave
]

n_hidden = [Nunits for i in range(Nlayer)]
n_wave = np.sum(wave_bin)

# -------------------------------------------------------
if model == "nmf":
    n_param = 10
elif model == "burst":
    n_param = 4
else:
    raise ValueError

# load trained PCA basis object
print("loading PCA bases")

fpca = os.path.join(
    dat_dir,
    "fsps.%s.v%s.seed0_%i%s.pca%i.hdf5"
    % (model, version, nbatch - 1, str_wbin, n_pcas),
)
PCABasis = SpectrumPCA(
    n_parameters=n_param,  # number of parameters
    n_wavelengths=n_wave,  # number of wavelength values
    n_pcas=n_pcas,  # number of pca coefficients to include in the basis
    spectrum_filenames=None,  # list of filenames containing the (un-normalized) log spectra for training the PCA
    parameter_filenames=[],  # list of filenames containing the corresponding parameter values
    parameter_selection=None,
)  # pass an optional function that takes in parameter vector(s) and returns True/False for any extra parameter cuts we want to impose on the training sample (eg we may want to restrict the parameter ranges)
PCABasis._load_from_file(fpca)

# -------------------------------------------------------
# training theta and pca
_thetas = np.load(fpca.replace(".hdf5", "_parameters.npy"))
_pcas = np.load(fpca.replace(".hdf5", "_pca.npy"))

if model == "nmf":
    _thetas = _thetas[:, 1:]
    n_param = 9
# elif model == 'burst':
#    # convert tburst and Z to log tburst and log Z
#    _thetas[:,0] = np.log10(_thetas[:,0])
#    _thetas[:,1] = np.log10(_thetas[:,1])

# get parameter shift and scale
theta_shift = tf.convert_to_tensor(np.mean(_thetas, axis=0).astype(np.float32))
theta_scale = tf.convert_to_tensor(np.std(_thetas, axis=0).astype(np.float32))

Ntrain = int(0.9 * _thetas.shape[0])
Nvalid = _thetas.shape[0] - Ntrain
print("Ntrain = %i, Nvalid = %i" % (Ntrain, Nvalid))

theta_train = tf.convert_to_tensor(_thetas[:Ntrain, :].astype(np.float32))
pca_train = tf.convert_to_tensor(_pcas[:Ntrain, :].astype(np.float32))

# validation theta and pca
theta_valid = tf.convert_to_tensor(_thetas[Ntrain:, :].astype(np.float32))
pca_valid = tf.convert_to_tensor(_pcas[Ntrain:, :].astype(np.float32))

# -------------------------------------------------------
# train Speculator
speculator = Speculator(
    n_parameters=n_param,  # number of model parameters
    wavelengths=wave[wave_bin],  # array of wavelengths
    pca_transform_matrix=PCABasis.pca_transform_matrix,
    parameters_shift=theta_shift,  # PCABasis.parameters_shift,
    parameters_scale=theta_scale,  # PCABasis.parameters_scale,
    pca_shift=PCABasis.pca_shift,
    pca_scale=PCABasis.pca_scale,
    spectrum_shift=PCABasis.spectrum_shift,
    spectrum_scale=PCABasis.spectrum_scale,
    n_hidden=n_hidden,  # network architecture (list of hidden units per layer)
    restore=False,
    optimizer=tf.keras.optimizers.Adam(),
)  # optimizer for model training

# cooling schedule
lr = [1e-3, 5e-4, 1e-4, 5e-5, 1e-5, 5e-6, 1e-6]
batch_size = [b_size for _ in lr]
gradient_accumulation_steps = [
    1 for _ in lr
]  # split the largest batch size into 10 when computing gradients to avoid memory overflow

# early stopping set up
patience = 20

# writeout loss
_floss = os.path.join(
    dat_dir,
    "%s.v%s.seed0_%i%s.pca%i.%ix%i.%s.loss.dat"
    % (model, version, nbatch - 1, str_wbin, n_pcas, Nlayer, Nunits, desc),
)
floss = open(_floss, "w")
floss.close()

# train using cooling/heating schedule for lr/batch-size
for i in range(len(lr)):
    print("learning rate = " + str(lr[i]) + ", batch size = " + str(batch_size[i]))

    # set learning rate
    speculator.optimizer.lr = lr[i]

    # create iterable dataset (given batch size)
    training_data = (
        tf.data.Dataset.from_tensor_slices((theta_train, pca_train))
        .shuffle(theta_train.shape[0])
        .batch(batch_size[i])
    )

    # set up training loss
    training_loss = [np.infty]
    validation_loss = [np.infty]
    best_loss = np.infty
    early_stopping_counter = 0

    # loop over epochs
    while early_stopping_counter < patience:

        # loop over batches
        train_loss, nb = 0, 0
        for theta, pca in training_data:
            # training step: check whether to accumulate gradients or not (only worth doing this for very large batch sizes)
            if gradient_accumulation_steps[i] == 1:
                train_loss += speculator.training_step(theta, pca)
            else:
                train_loss += speculator.training_step_with_accumulated_gradients(
                    theta, pca, accumulation_steps=gradient_accumulation_steps[i]
                )
            nb += 1
        train_loss /= float(nb)
        training_loss.append(train_loss)

        # compute validation loss at the end of the epoch
        validation_loss.append(speculator.compute_loss(theta_valid, pca_valid).numpy())

        floss = open(_floss, "a")  # append
        floss.write(
            "%i \t %f \t %f \t %f\n"
            % (batch_size[i], lr[i], train_loss, validation_loss[-1])
        )
        floss.close()

        # early stopping condition
        if validation_loss[-1] < best_loss:
            best_loss = validation_loss[-1]
            early_stopping_counter = 0
        else:
            early_stopping_counter += 1

        if early_stopping_counter >= patience:
            speculator.update_emulator_parameters()
            speculator.save(
                os.path.join(
                    dat_dir,
                    "%s.v%s.seed0_%i%s.pca%i.%ix%i.%s"
                    % (
                        model,
                        version,
                        nbatch - 1,
                        str_wbin,
                        n_pcas,
                        Nlayer,
                        Nunits,
                        desc,
                    ),
                )
            )
            print("Validation loss = %s" % str(best_loss))

