'''


module for sps models 


'''
import os 
import h5py 
import pickle
import warnings
import numpy as np 
from scipy.stats import sigmaclip
from scipy.special import gammainc
import scipy.interpolate as Interp
# --- astropy --- 
from astropy import units as U
from astropy.cosmology import Planck13
# --- gqp_mc --- 
from . import util as UT

try: 
    import fsps
except ImportError:
    warnings.warn('import error with fsps; only use emulators')


class Model(object): 
    ''' Base class object for different SPS models. Different `Model` objects
    specify different SPS model. The primary purpose of the `Model` class is to
    evaluate the SED given a set of parameter values. 
    '''
    def __init__(self, cosmo=None, **kwargs): 

        self._init_model(**kwargs)
        
        if cosmo is None: 
            self.cosmo = Planck13 # cosmology  

        # interpolators for speeding up cosmological calculations 
        _z = np.linspace(0.0, 0.5, 100)
        _tage = self.cosmo.age(_z).value
        _d_lum_cm = self.cosmo.luminosity_distance(_z).to(U.cm).value # luminosity distance in cm

        self._tage_z_interp = \
                Interp.InterpolatedUnivariateSpline(_z, _tage, k=3)
        self._z_tage_interp = \
                Interp.InterpolatedUnivariateSpline(_tage[::-1], _z[::-1], k=3)
        self._d_lum_z_interp = \
                Interp.InterpolatedUnivariateSpline(_z, _d_lum_cm, k=3)
        print('input parameters : %s' % ', '.join(self._parameters))
    
    def sed(self, tt, zred, vdisp=0., wavelength=None, resolution=None,
            filters=None, debug=False):
        ''' compute the redshifted spectral energy distribution (SED) for a
        given set of parameter values and redshift.
       

        Parameters
        ----------
        tt : 2-d array
            [Nsample,Nparam] SPS parameters     

        zred : float or array_like
            redshift of the SED 

        vdisp : float or array_like
            velocity dispersion  

        wavelength : array_like[Nwave,]
            If you want to use your own wavelength. If specified, the model
            will interpolate the spectra to the specified wavelengths. By
            default, it will use the speculator wavelength
            (Default: None)  

        resolution : array_like[N,Nwave]
            resolution matrix (e.g. DESI data provides a resolution matrix)  
    
        filters : object
            Photometric bandpass filter to generate photometry.
            `speclite.FilterResponse` object. 

        debug: boolean
            If True, prints out a number lines to help debugging 


        Returns
        -------
        outwave : [Nsample, Nwave]
            output wavelengths in angstrom. 

        outspec : [Nsample, Nwave]
            the redshifted SED in units of 1e-17 * erg/s/cm^2/Angstrom.
        '''
        tt      = np.atleast_2d(tt)
        zred    = np.atleast_1d(zred) 
        vdisp   = np.atleast_1d(vdisp) 
        ntheta  = tt.shape[1]

        assert tt.shape[0] == zred.shape[0]
       
        outwave, outspec, maggies = [], [], [] 
        for _tt, _zred in zip(tt, zred): 

            if debug: print('Model.sed: redshift = %f' % _zred)
            _tage = self.cosmo.age(_zred).value 

            # get SSP luminosity
            wave_rest, lum_ssp = self._sps_model(_tt, _tage)
            if debug: print('Model.sed: ssp lum', lum_ssp)

            # redshift the spectra
            w_z = wave_rest * (1. + _zred)
            d_lum = self._d_lum_z_interp(_zred) 
            flux_z = lum_ssp * UT.Lsun() / (4. * np.pi * d_lum**2) / (1. + _zred) * 1e17 # 10^-17 ergs/s/cm^2/Ang

            # apply velocity dispersion 
            wave_smooth, flux_smooth = self._apply_vdisp(w_z, flux_z, vdisp)
            
            if wavelength is None: 
                outwave.append(wave_smooth)
                outspec.append(flux_smooth)
            else: 
                outwave.append(wavelength)

                # resample flux to input wavelength  
                resampflux = UT.trapz_rebin(wave_smooth, flux_smooth, xnew=wavelength) 

                if resolution is not None: 
                    # apply resolution matrix 
                    _i = 0 
                    for res in np.atleast_1d(resolution):
                        _res = UT.Resolution(res) 
                        resampflux[_i:_i+res.shape[-1]] = _res.dot(resampflux[_i:_i+res.shape[-1]]) 
                        _i += res.shape[-1]
                outspec.append(resampflux) 

            if filters is not None: 
                # calculate photometry from SEDs 
                flux_z, w_z = filters.pad_spectrum(np.atleast_2d(flux_z) *
                        1e-17*U.erg/U.s/U.cm**2/U.Angstrom,
                        w_z * U.Angstrom)
                _maggies = filters.get_ab_maggies(flux_z, wavelength=w_z)
                maggies.append(np.array(list(_maggies[0])) * 1e9)

        if len(outwave) == 1: 
            outwave = outwave[0] 
            outspec = outspec[0] 
            if filters is not None: maggies = maggies[0]
        else: 
            outwave = np.array(outwave)
            outspec = np.array(outspec) 
            if filters is not None: maggies = np.array(maggies)

        if filters is None: 
            return outwave, outspec
        else: 
            return outwave, outspec, maggies
    
    def _init_model(self, **kwargs) : 
        return None 

    def _apply_vdisp(self, wave, flux, vdisp): 
        ''' apply velocity dispersion by first rebinning to log-scale
        wavelength then convolving vdisp. 

        Notes
        -----
        * code lift from https://github.com/desihub/desigal/blob/d67a4350bc38ae42cf18b2db741daa1a32511f8d/py/desigal/nyxgalaxy.py#L773
        * confirmed that it reproduces the velocity dispersion calculations in
        prospector
        (https://github.com/bd-j/prospector/blob/41cbdb7e6a13572baea59b75c6c10100e7c7e212/prospect/utils/smoothing.py#L17)
        '''
        if vdisp <= 0: 
            return wave, flux
        from scipy.ndimage import gaussian_filter1d
        pixkms = 10.0                                 # SSP pixel size [km/s]
        dlogwave = pixkms / 2.998e5 / np.log(10)
        wlog = 10**np.arange(np.log10(wave.min() + 10.), np.log10(wave.max() - 10.), dlogwave)
        flux_wlog = UT.trapz_rebin(wave, flux, xnew=wlog, edges=None)
        # convolve  
        sigma = vdisp / pixkms # in pixels 
        smoothflux = gaussian_filter1d(flux_wlog, sigma=sigma, axis=0)
        return wlog, smoothflux
    
    def _parse_theta(self, tt):
        ''' parse given array of parameter values 
        '''
        tt = np.atleast_2d(tt.copy()) 

        assert tt.shape[1] == len(self._parameters), 'given theta has %i instead of %i dims' % (tt.shape[1], len(self._parameters))

        theta = {} 
        for i, param in enumerate(self._parameters): 
            theta[param] = tt[:,i]
        return theta 


class NMF(Model): 
    ''' SPS model with non-parametric star formation and metallicity histories
    and flexible dust attenuation model. The SFH and ZH are based on non-negative
    matrix factorization (NMF) bases (Tojeiro+in prep). The dust attenuation
    uses a standard Charlot & Fall dust model.

    The SFH uses 4 NMF bases. If you specify `burst=True`, the SFH will
    include an additional burst component. 
    
    The ZH uses 2 NMF bases. Minimum metallicities of 4.49e-5 and 4.49e-2 are
    imposed automatically on the ZH. These limits are based on the metallicity
    limits of the MIST isochrones.

    The dust attenuation is modeled using a 3 parameter Charlot & Fall model.
    `dust1` is tau_BC, the optical depth of dust attenuation of birth cloud
    that only affects young stellar population. `dust2` is tau_ISM, the optical
    depth of dust attenuation from the ISM, which affects all stellar emission.
    `dust_index` is the attenuation curve slope offset from Calzetti.
    
    If you specify `emulator=True`, the model will use a PCA NN emulator to
    evaluate the SPS model, rather than Flexible Stellar Population Synthesis.
    The emulator has <1% level accuracy and its *much* faster than FSPS. I
    recommend using the emulator for parameter exploration. 


    Parameters
    ----------
    burst : bool
        If True include a star bursts component in SFH. (default: True) 

    emulator : bool
        If True, use emulator rather than running FSPS. 

    cosmo : astropy.comsology object
        specify cosmology. If cosmo=None, NMF uses astorpy.cosmology.Planck13 
        by default.


    Notes 
    -----
    * only supports 4 component SFH with or without burst and 2 component ZH 
    * only supports Calzetti+(2000) attenuation curve) and Chabrier IMF. 
    '''
    def __init__(self, burst=True, emulator=False, cosmo=None): 
        self._ssp = None 
        self._burst = burst
        self._emulator = emulator 
        # metallicity range set by MIST isochrone
        self._Z_min = 4.49043431e-05
        self._Z_max = 4.49043431e-02
        super().__init__(cosmo=cosmo) # initializes the model

    def _emu(self, tt, tage): 
        ''' PCA neural network emulator of FSPS SED model. If `emulator=True`,
        this emulator is used instead of `_fsps`. The emulator is *much* faster
        than the FSPS.

        Parameters 
        ----------
        tt : 1d array 
            Nparam array that specifies the parameter values 

        tage : float 
            age of galaxy 
        
        Returns
        -------
        wave_rest : array_like[Nwave] 
            rest-frame wavelength of SSP flux 

        lum_ssp : array_like[Nwave] 
            FSPS SSP luminosity in units of Lsun/A
    

        Notes
        -----
        * June 11, 2021: burst component no longer uses an emulator because
            it's fast enough.
        '''
        theta = self._parse_theta(tt) 
        
        assert np.isclose(np.sum([theta['beta1_sfh'], theta['beta2_sfh'],
            theta['beta3_sfh'], theta['beta4_sfh']]), 1.), "SFH basis coefficients should add up to 1"
    
        # get redshift with interpolation 
        zred = self._z_tage_interp(tage) 
    
        tt_nmf = np.concatenate([theta['beta1_sfh'], theta['beta2_sfh'],
            theta['beta3_sfh'], theta['beta4_sfh'], theta['gamma1_zh'],
            theta['gamma2_zh'], theta['dust1'], theta['dust2'],
            theta['dust_index'], [zred]])

        # NMF from emulator 
        lum_ssp = np.exp(self._emu_nmf(tt_nmf)) 
    
        # add burst contribution 
        if self._burst: 
            fburst = theta['fburst']
            tburst = theta['tburst'] 

            lum_burst = np.zeros(lum_ssp.shape)
            # if starburst is within the age of the galaxy 
            if tburst < tage and fburst > 0.: 
                lum_burst = np.exp(self._emu_burst(tt))
                #_w, _lum_burst = self._fsps_burst(tt)
                #lum_burst = _lum_burst[(_w > 2300.) & (_w < 60000.)]

            # renormalize NMF contribution  
            lum_ssp *= (1. - fburst) 

            # add in burst contribution 
            lum_ssp += fburst * lum_burst

        # normalize by stellar mass 
        lum_ssp *= (10**theta['logmstar'])

        return self._nmf_emu_waves, lum_ssp

    def _fsps(self, tt, tage): 
        ''' FSPS SED model. If `emulator=False`, FSPS is used to evaluate the
        SED rather than the emulator. First, SFH and ZH are constructed from
        the `tt` parameters. Then stellar population synthesis is used to get
        the spectra of each time bin. Afterwards, they're combined to construct
        the SED. 

        Parameters 
        ----------
        tt : 1d array 
            Nparam array that specifies the parameter values 

        tage : float 
            age of galaxy 
        

        Returns
        -------
        wave_rest : array_like[Nwave] 
            rest-frame wavelength of SSP flux 


        lum_ssp : array_like[Nwave] 
            FSPS SSP luminosity in units of Lsun/A

        Notes
        -----
        * 12/23/2020: age of SSPs are no longer set to the center of the
          lookback time as this  ignores the youngest tage ~ 0 SSP, which have
          significant contributions. Also we set a minimum tage = 1e-8 because
          FSPS returns a grid for tage = 0 but 1e-8 gets the youngest isochrone
          anyway. 
        * 2021/06/24: log-spaced lookback time implemented
        '''
        if self._ssp is None: self._ssp_initiate()  # initialize FSPS StellarPopulation object
        theta = self._parse_theta(tt) 
        
        assert np.isclose(np.sum([theta['beta1_sfh'], theta['beta2_sfh'],
            theta['beta3_sfh'], theta['beta4_sfh']]), 1.), "SFH basis coefficients should add up to 1"
        
        # NMF SFH(t) noramlized to 1 **without burst**
        tlb_edges, sfh = self.SFH(
                np.concatenate([[0.], tt[1:]]), 
                tage=tage, _burst=False)  
        tages = 0.5 * (tlb_edges[1:] + tlb_edges[:-1])
        # NMF ZH at lookback time bins 
        _, zh = self.ZH(tt, tage=tage)
        
        dt = np.diff(tlb_edges)
    
        # look over log-spaced lookback time bins and add up SSPs
        for i, tage in enumerate(tages): 
            m = dt[i] * sfh[i] # mass formed in this bin 
            if m == 0 and i != 0: continue 

            self._ssp.params['logzsol'] = np.log10(zh[i]/0.0190) # log(Z/Zsun)
            self._ssp.params['dust1'] = theta['dust1']
            self._ssp.params['dust2'] = theta['dust2']  
            self._ssp.params['dust_index'] = theta['dust_index']
            
            wave_rest, lum_i = self._ssp.get_spectrum(tage=tage, peraa=True) # in units of Lsun/AA
            # note that this spectrum is normalized such that the total formed
            # mass = 1 Msun

            if i == 0: lum_ssp = np.zeros(len(wave_rest))
            lum_ssp += m * lum_i 
    
        # add burst contribution 
        if self._burst: 
            fburst = theta['fburst']
            tburst = theta['tburst'] 

            lum_burst = np.zeros(lum_ssp.shape)
            # if starburst is within the age of the galaxy 
            if tburst < tage: 
                _, lum_burst = self._fsps_burst(tt)

            # renormalize NMF contribution  
            lum_ssp *= (1. - fburst) 

            # add in burst contribution 
            lum_ssp += fburst * lum_burst

        # normalize by stellar mass 
        lum_ssp *= (10**theta['logmstar'])

        return wave_rest, lum_ssp

    def _fsps_burst(self, tt, debug=False):
        ''' dust attenuated spectra of single stellar population that
        corresponds to the burst. The spectrum is normalized such that the
        total formed mass is 1 Msun, **not** fburst. The spectrum is calculated
        using FSPS. 
        '''
        if self._ssp is None: self._ssp_initiate()  # initialize FSPS StellarPopulation object
        theta = self._parse_theta(tt) 
        tt_zh = np.array([theta['gamma1_zh'], theta['gamma2_zh']])

        tburst = theta['tburst'] 
        assert tburst > 1e-2, "burst currently only supported for tburst > 1e-2 Gyr"

        dust1           = 0. # no birth cloud attenuation for tage > 1e-2 Gyr
        dust2           = theta['dust2']
        dust_index      = theta['dust_index']

        # get metallicity at tburst 
        zburst = np.sum(np.array([tt_zh[i] * self._zh_basis[i](tburst) 
            for i in range(self._N_nmf_zh)])).clip(self._Z_min, self._Z_max) 
        
        if debug:
            print('zburst=%e' % zburst) 
            #print('dust1=%f' % dust1) 
            print('dust2=%f' % dust2) 
            print('dust_index=%f' % dust_index) 
    
        # luminosity of SSP at tburst 
        self._ssp.params['logzsol'] = np.log10(zburst/0.0190) # log(Z/Zsun)
        self._ssp.params['dust1'] = dust1
        self._ssp.params['dust2'] = dust2 
        self._ssp.params['dust_index'] = dust_index
        
        wave_rest, lum_burst = self._ssp.get_spectrum(
                tage=np.clip(tburst, 1e-8, None), 
                peraa=True) # in units of Lsun/AA
        # note that this spectrum is normalized such that the total formed
        # mass = 1 Msun
        return wave_rest, lum_burst

    def _emu_nmf(self, tt):
        ''' emulator for the SED from the NMF star formation history.
        
        Parameters
        ----------
        tt : 1d array 
            Nparam array that specifies 
            [beta1_sfh, beta2_sfh, beta3_sfh, beta4_sfh, gamma1_zh, gamma2_zh, dust1, dust2, dust_index, redshift] 
    
        Returns
        -------
        logflux : array_like[Nwave,] 
            (natural) log of (SSP luminosity in units of Lsun/A)
        '''
        # untransform SFH coefficients from Dirichlet distribution 
        _tt = np.empty(9)
        _tt[0] = (1. - tt[0]).clip(1e-8, None)
        for i in range(1,3): 
            _tt[i] = 1. - (tt[i] / np.prod(_tt[:i]))
        _tt[3:] = tt[4:]

        logflux = [] 
        for iwave in range(self._nmf_n_emu): # wave bins
            W_, b_, alphas_, betas_, parameters_shift_, parameters_scale_,\
                    pca_shift_, pca_scale_, spectrum_shift_, spectrum_scale_,\
                    pca_transform_matrix_, _, _, wavelengths, _, _, n_layers, _ =\
                    self._nmf_emu_params[iwave] 

            # forward pass through the network
            act = []
            layers = [(_tt - parameters_shift_)/parameters_scale_]
            for i in range(n_layers-1):

                # linear network operation
                act.append(np.dot(layers[-1], W_[i]) + b_[i])

                # pass through activation function
                layers.append((betas_[i] + (1.-betas_[i])*1./(1.+np.exp(-alphas_[i]*act[-1])))*act[-1])

            # final (linear) layer -> (normalized) PCA coefficients
            layers.append(np.dot(layers[-1], W_[-1]) + b_[-1])

            # rescale PCA coefficients, multiply out PCA basis -> normalized spectrum, shift and re-scale spectrum -> output spectrum
            logflux.append(np.dot(layers[-1]*pca_scale_ + pca_shift_,
                pca_transform_matrix_)*spectrum_scale_ + spectrum_shift_)

        return np.concatenate(logflux) 
   
    def _emu_burst(self, tt, debug=False): 
        ''' calculate the dust attenuated luminosity contribution from a SSP
        that corresponds to the burst using an emulator. This spectrum is
        normalized such that the total formed mass is 1 Msun, **not** fburst 

        Notes
        -----
        * currently luminosity contribution is set to 0 if tburst > 13.27 due
        to FSPS numerical accuracy  
        '''
        theta = self._parse_theta(tt) 
        tt_zh = np.array([theta['gamma1_zh'], theta['gamma2_zh']])

        tburst = theta['tburst'] 

        if tburst > 13.27: 
            warnings.warn('tburst > 13.27 Gyr returns 0s --- modify priors')
            return np.zeros(len(self._nmf_emu_waves))
        assert tburst > 1e-2, "burst currently only supported for tburst > 1e-2 Gyr"

        #dust1           = theta['dust1']
        dust2           = theta['dust2']
        dust_index      = theta['dust_index']

        # get metallicity at tburst 
        zburst = np.sum(np.array([tt_zh[i] * self._zh_basis[i](tburst) 
            for i in range(self._N_nmf_zh)])).clip(self._Z_min, self._Z_max) 

        # input to emulator are [log tburst, kburst, dust2, dust_index]
        tt = np.array([
            np.log10(tburst), 
            np.log10([zburst]), 
            theta['dust2'], 
            theta['dust_index']]).flatten()

        logflux = [] 
        for iwave in range(self._burst_n_emu): # wave bins
            W_, b_, alphas_, betas_, parameters_shift_, parameters_scale_,\
                    pca_shift_, pca_scale_, spectrum_shift_, spectrum_scale_,\
                    pca_transform_matrix_, _, _, wavelengths, _, _, n_layers, _ =\
                    self._burst_emu_params[iwave] 

            # forward pass through the network
            act = []
            layers = [(tt - parameters_shift_)/parameters_scale_]
            for i in range(n_layers-1):

                # linear network operation
                act.append(np.dot(layers[-1], W_[i]) + b_[i])

                # pass through activation function
                layers.append((betas_[i] + (1.-betas_[i])*1./(1.+np.exp(-alphas_[i]*act[-1])))*act[-1])

            # final (linear) layer -> (normalized) PCA coefficients
            layers.append(np.dot(layers[-1], W_[-1]) + b_[-1])

            # rescale PCA coefficients, multiply out PCA basis -> normalized spectrum, shift and re-scale spectrum -> output spectrum
            logflux.append(np.dot(layers[-1]*pca_scale_ + pca_shift_,
                pca_transform_matrix_)*spectrum_scale_ + spectrum_shift_)
        return np.concatenate(logflux) 

    def _load_emulator(self): 
        ''' read in pickle files that contains the parameters for the FSPS
        emulator that is split into wavelength bins
        '''
        # load NMF emulator 
        npcas = [30, 50, 50, 30]
        f_nn = lambda npca, i: 'fsps.nmf.seed0_99.w%i.pca%i.8x256.nbatch250.pkl' % (i, npca)
        
        self._nmf_n_emu         = len(npcas)
        self._nmf_emu_params    = [] 
        self._nmf_emu_wave      = [] 

        for i, npca in enumerate(npcas): 
            fpkl = open(os.path.join(
                os.path.dirname(os.path.realpath(__file__)), 'dat', 
                f_nn(npca, i)), 'rb')
            params = pickle.load(fpkl)

            self._nmf_emu_params.append(params)
            self._nmf_emu_wave.append(params[13])
        
        self._nmf_emu_waves = np.concatenate(self._nmf_emu_wave) 

        # load burst emulator
        f_nn = lambda npca, i: 'fsps.burst.seed0_199.w%i.pca%i.8x256.nbatch250.pkl' % (i, npca)
        self._burst_n_emu = len(npcas)
        self._burst_emu_params    = [] 
        self._burst_emu_wave      = [] 

        for i, npca in enumerate(npcas): 
            fpkl = open(os.path.join(
                os.path.dirname(os.path.realpath(__file__)), 'dat', 
                f_nn(npca, i)), 'rb')
            params = pickle.load(fpkl)

            self._burst_emu_params.append(params)
            self._burst_emu_wave.append(params[13])

        self._burst_emu_waves = np.concatenate(self._burst_emu_wave) 
        return None 

    def SFH(self, tt, zred=None, tage=None, _burst=True): 
        ''' star formation history for given set of parameter values and
        redshift.
    
        Parameters
        ----------
        tt : 1d or 2d array 
            Nparam or NxNparam array of parameter values 

        tage : float
            age of the galaxy 

        Returns
        -------
        tedges: 1d array 
            bin edges of log-spaced look back time

        sfh : 1d or 2d array 
            star formation history at lookback time specified by t 
        '''
        if zred is None and tage is None: 
            raise ValueError("specify either the redshift or age of the galaxy")
        if tage is None: 
            assert isinstance(zred, float)
            tage = self.cosmo.age(zred).value # age in Gyr

        theta = self._parse_theta(tt) 

        # sfh nmf basis coefficients 
        tt_sfh = np.array([theta['beta1_sfh'], theta['beta2_sfh'],
            theta['beta3_sfh'], theta['beta4_sfh']]).T
    
        # log-spaced lookback time bin edges 
        tlb_edges = UT.tlookback_bin_edges(tage)

        sfh_hr = np.sum(np.array([tt_sfh[:,i][:,None] *
            self._sfh_basis_hr[i][None,:] for i in range(self._N_nmf_sfh)]),
            axis=0)

        sfh = np.array([UT.trapz_rebin(self._t_lb_hr, _sfh_hr, edges=tlb_edges)
            for _sfh_hr in sfh_hr])

        dt = np.diff(tlb_edges)
        sfh /= np.sum(dt * sfh, axis=1)[:,None]
        
        # add starburst 
        if self._burst and _burst: 
            fburst = theta['fburst'] # fraction of stellar mass from star burst
            tburst = theta['tburst'] # time of star burst
       
            noburst = (tburst > tage)
            fburst[noburst] = 0. 

            # add normalized starburst to SFH 
            sfh *= (1. - fburst)[:,None] 
            sfh += fburst[:,None] * self._SFH_burst(tburst, tlb_edges)

        # multiply by stellar mass 
        sfh *= 10**theta['logmstar'][:,None]

        if np.atleast_2d(tt).shape[0] == 1: 
            return tlb_edges, sfh[0]
        return tlb_edges, sfh 

    def _SFH_burst(self, tburst, tedges): 
        ''' place a single star-burst event on a given *evenly spaced* lookback time grid
        '''
        tburst = np.atleast_1d(tburst)
        dts = np.diff(tedges)
        
        # burst within the age of the galaxy 
        has_burst = (tburst < tedges.max()) 
        
        # log-spaced lookback time bin with burst 
        iburst = np.digitize(tburst[has_burst], tedges)-1

        sfh = np.zeros((len(tburst), len(tedges)-1))
        sfh[has_burst, iburst] += 1. / dts[iburst]
        return sfh 
   
    def avgSFR(self, tt, zred=None, tage=None, dt=1):
        ''' given a set of parameter values `tt` and redshift `zred`, calculate
        SFR averaged over `dt` Gyr. 

        parameters
        ----------
        tt : array_like[Ntheta, Nparam]
           Parameter values of [log M*, b1SFH, b2SFH, b3SFH, b4SFH, g1ZH, g2ZH,
           'dust1', 'dust2', 'dust_index']. 

        zred : float 
            redshift

        tage : float 
            age of galaxy 

        dt : float
            Gyrs to average the SFHs 
        '''
        if zred is None and tage is None: 
            raise ValueError('specify either zred or tage')
        if zred is not None and tage is not None: 
            raise ValueError('specify either zred or tage')
        if tage is None: 
            assert isinstance(zred, float)
            tage = self.cosmo.age(zred).value # age in Gyr

        theta = self._parse_theta(tt) 

        # sfh nmf basis coefficients 
        tt_sfh = np.array([theta['beta1_sfh'], theta['beta2_sfh'],
            theta['beta3_sfh'], theta['beta4_sfh']]).T
    
        # log-spaced lookback time bin edges 
        tlb_edges = UT.tlookback_bin_edges(tage)
        assert dt < tlb_edges[-1]

        sfh_hr = np.sum(np.array([tt_sfh[:,i][:,None] *
            self._sfh_basis_hr[i][None,:] for i in range(self._N_nmf_sfh)]),
            axis=0)

        sfh = np.array([UT.trapz_rebin(self._t_lb_hr, _sfh_hr, edges=tlb_edges)
            for _sfh_hr in sfh_hr])
        sfh /= np.sum(np.diff(tlb_edges) * sfh, axis=1)[:,None] # normalize 
      
        i_dt = np.digitize(dt, tlb_edges) - 1
        avg_sfr = np.sum(np.diff(tlb_edges)[:i_dt][None,:] * sfh[:,:i_dt], axis=1) 
        avg_sfr += (dt - tlb_edges[i_dt]) * sfh[:,i_dt]
        
        # add starburst event 
        if self._burst: 
            fburst = theta['fburst'] # fraction of stellar mass from star burst
            tburst = theta['tburst'] # time of star burst
       
            noburst = (tburst > dt)
            fburst[noburst] = 0. 
            
            avg_sfr *= (1. - fburst)
            avg_sfr += fburst

        # multiply by stellar mass 
        avg_sfr *= 10**theta['logmstar']
        return avg_sfr

    def ZH(self, tt, zred=None, tage=None): 
        ''' metallicity history for given set of parameters. metallicity is
        parameterized using a 2 component NMF basis. The parameter values
        specify the coefficient of the components and this method returns the
        linear combination of the two. 

        parameters
        ----------
        tt : array_like[N,Nparam]
           parameter values of the model 

        zred : float
            redshift of galaxy/csp

        Returns 
        -------
        tedge : 1d array
            bin edges of lookback time

        zh : 2d array
            metallicity at cosmic time t --- ZH(t) 
        '''
        if zred is None and tage is None: 
            raise ValueError("specify either the redshift or age of the galaxy")
        if tage is None: 
            assert isinstance(zred, float)
            tage = self.cosmo.age(zred).value # age in Gyr
        theta = self._parse_theta(tt) 

        # metallicity history basis coefficients  
        tt_zh = np.array([theta['gamma1_zh'], theta['gamma2_zh']]).T

        # log-spaced lookback time bin edges 
        tlb_edges = UT.tlookback_bin_edges(tage)
        tlb = 0.5 * (tlb_edges[1:] + tlb_edges[:-1])

        # metallicity basis
        _z_basis = np.array([self._zh_basis[i](tlb) for i in range(self._N_nmf_zh)]) 

        # get metallicity history
        zh = np.sum(np.array([tt_zh[:,i][:,None] * _z_basis[i][None,:] for i in
            range(self._N_nmf_zh)]), axis=0).clip(self._Z_min, self._Z_max) 

        if tt_zh.shape[0] == 1: return tlb_edges, zh[0]
        return tlb_edges, zh 
    
    def Z_MW(self, tt, tage=None, zred=None):
        ''' given theta calculate mass weighted metallicity using the ZH NMF
        bases. 
        '''
        if tage is None and zred is None: 
            raise ValueError("specify either zred or tage") 
        if tage is not None and zred is not None: 
            raise ValueError("specify either zred or tage") 

        theta = self._parse_theta(tt) 
        tlb_edge, sfh = self.SFH(tt, tage=tage, zred=zred) # get SFH 
        _, zh = self.ZH(tt, tage=tage, zred=zred) 

        # mass weighted average
        z_mw = np.sum(np.diff(tlb_edge)[None,:] * sfh * zh, axis=1) / (10**theta['logmstar']) 
        return z_mw 

    def _load_NMF_bases(self, name='tojeiro.4comp'): 
        ''' read in NMF SFH and ZH bases. These bases are used to reduce the
        dimensionality of the SFH and ZH. 
        '''
        dir_dat = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'dat') 
        if name == 'tojeiro.4comp': 
            fsfh = os.path.join(dir_dat, 'NMF_2basis_SFH_components_nowgt_lin_Nc4.txt')
            fzh = os.path.join(dir_dat, 'NMF_2basis_Z_components_nowgt_lin_Nc2.txt') 
            ft = os.path.join(dir_dat, 'sfh_t_int.txt') 

            nmf_sfh = np.loadtxt(fsfh)[:,::-1] # basis order is jumbled up it should be [2 ,0, 1, 3]
            nmf_zh  = np.loadtxt(fzh)[:,::-1] 
            nmf_t   = np.loadtxt(ft)[::-1] # look back time 

            self._nmf_t_lb_sfh      = nmf_t 
            self._nmf_t_lb_zh       = nmf_t 
            self._nmf_sfh_basis     = np.array([nmf_sfh[2], nmf_sfh[0], nmf_sfh[1], nmf_sfh[3]])
            self._nmf_zh_basis      = nmf_zh
        elif name in ['tng.4comp', 'tng.6comp']: 
            icomp = int(name.split('.')[-1][0])
            fsfh = os.path.join(dir_dat, 'NMF_basis.sfh.tng%icomp.txt' % icomp) 
            fzh = os.path.join(dir_dat, 'NMF_2basis_Z_components_nowgt_lin_Nc2.txt') 
            ftsfh = os.path.join(dir_dat, 't_sfh.tng%icomp.txt' % icomp) 
            ftzh = os.path.join(dir_dat, 'sfh_t_int.txt') 

            nmf_sfh     = np.loadtxt(fsfh, unpack=True) 
            nmf_zh      = np.loadtxt(fzh) 
            nmf_tsfh    = np.loadtxt(ftsfh) # look back time 
            nmf_tzh     = np.loadtxt(ftzh) # look back time 

            self._nmf_t_lb_sfh      = nmf_tsfh
            self._nmf_t_lb_zh       = nmf_tzh[::-1]
            self._nmf_sfh_basis     = nmf_sfh 
            self._nmf_zh_basis      = nmf_zh[:,::-1]
        else:
            raise NotImplementedError

        self._Ncomp_sfh = self._nmf_sfh_basis.shape[0]
        self._Ncomp_zh = self._nmf_zh_basis.shape[0]
        
        # SFH bases as a function of lookback time 
        self._sfh_basis = [
                Interp.InterpolatedUnivariateSpline(
                    self._nmf_t_lb_sfh, 
                    self._nmf_sfh_basis[i], k=1) 
                for i in range(self._Ncomp_sfh)
                ]
        self._zh_basis = [
                Interp.InterpolatedUnivariateSpline(
                    self._nmf_t_lb_zh, 
                    self._nmf_zh_basis[i], k=1) 
                for i in range(self._Ncomp_zh)]
        
        # high resolution tabulated SFHs used in the SFH calculation
        self._t_lb_hr       = np.linspace(0., 13.8, int(5e4))
        self._sfh_basis_hr  = [sfh_basis(self._t_lb_hr) for sfh_basis in self._sfh_basis]
        return None 

    def _init_model(self, **kwargs): 
        ''' some under the hood initalization of the model and its
        parameterization. 
        '''
        # load 4 component NMF bases from Rita 
        self._load_NMF_bases(name='tojeiro.4comp')
        self._N_nmf_sfh = 4 # 4 NMF component SFH
        self._N_nmf_zh  = 2 # 2 NMF component ZH 

        if not self._burst:
            self._parameters = [
                    'logmstar', 
                    'beta1_sfh', 
                    'beta2_sfh', 
                    'beta3_sfh',
                    'beta4_sfh', 
                    'gamma1_zh', 
                    'gamma2_zh', 
                    'dust1', 
                    'dust2',
                    'dust_index']
        else: 
            self._parameters = [
                    'logmstar', 
                    'beta1_sfh', 
                    'beta2_sfh', 
                    'beta3_sfh',
                    'beta4_sfh', 
                    'fburst', 
                    'tburst',   # lookback time of the universe when burst occurs (tburst < tage) 
                    'gamma1_zh', 
                    'gamma2_zh', 
                    'dust1', 
                    'dust2',
                    'dust_index']

        if not self._emulator: 
            self._sps_model = self._fsps 
        else: 
            self._sps_model = self._emu
            self._load_emulator()

        return None 

    def _ssp_initiate(self): 
        ''' initialize sps (FSPS StellarPopulaiton object) 
        '''
        sfh         = 0 # tabulated SFH
        dust_type   = 4 # dust1, dust2, and dust_index 
        imf_type    = 1 # chabrier

        self._ssp = fsps.StellarPopulation(
                zcontinuous=1,          # interpolate metallicities
                sfh=sfh,                # sfh type 
                dust_type=dust_type,            
                imf_type=imf_type)             # chabrier 
        return None  
    
"""
    class Tau(Model): 
        ''' SPS model where SFH is parameterized using tau models (standard or
        delayed tau) and constant metallicity history.  

        Parameters
        ----------
        burst : bool
            If True include a star bursts in SFH. (default: True) 

        delayed : bool
            If False, use standard tau model. 
            If True, use delayed-tau model. 
            (default: False) 

        emulator : bool
            If True, use emulator rather than running FSPS. Not yet implemented for
            `Tau`. (default: False) 

        cosmo : astropy.comsology object
            specify cosmology


        Notes 
        -----
        * only supports Calzetti+(2000) attenuation curve) and Chabrier IMF. 

        '''
        def __init__(self, burst=True, delayed=False, emulator=False, cosmo=None): 
            self._ssp = None 
            self._burst = burst 
            self._delayed = delayed
            self._emulator = emulator 
            assert not emulator, "emulator not yet implemneted --- coming soon"
            super().__init__(cosmo=cosmo)

        def _fsps(self, tt, tage): 
            ''' FSPS SPS model with tau or delayed-tau model SFH  


            Parameters 
            ----------
            tt : 1-d array
                Parameter FSPS tau model 
                * log M*  
                * e-folding time in Gyr 0.1 < tau < 10^2
                * constant component 0 < C < 1 
                * start time of SFH in Gyr

                if burst = True
                * fraction of mass formed in an instantaneous burst of star formation
                * age of the universe when burst occurs (tburst < tage) 

                * metallicity 
                * Calzetti+(2000) dust index 
            
            tage : float
                age of the galaxy 

            Returns
            -------
            wave_rest : array_like[Nwave] 
                rest-frame wavelength of SSP flux 


            lum_ssp : array_like[Nwave] 
                FSPS SSP luminosity in units of Lsun/A
            '''
            # initialize FSPS StellarPopulation object
            if self._ssp is None: self._ssp_initiate() 
            theta = self._parse_theta(tt) 

            # sfh parameters
            self._ssp.params['tau']      = theta['tau_sfh'] # e-folding time in Gyr 0.1 < tau < 10^2
            self._ssp.params['const']    = theta['const_sfh'] # constant component 0 < C < 1 
            self._ssp.params['sf_start'] = theta['sf_start'] # start time of SFH in Gyr
            if self._burst: 
                self._ssp.params['fburst'] = theta['fburst'] # fraction of mass formed in an instantaneous burst of star formation
                self._ssp.params['tburst'] = theta['tburst'] # age of the universe when burst occurs (tburst < tage) 

            # metallicity
            self._ssp.params['logzsol']  = np.log10(theta['metallicity']/0.0190) # log(z/zsun) 
            # dust 
            self._ssp.params['dust2']    = theta['dust2']  # dust2 parameter in fsps 
            
            w, l_ssp = self._ssp.get_spectrum(tage=tage, peraa=True) 
            
            # mass normalization
            l_ssp *= (10**theta['logmstar']) 

            return w, l_ssp 

        def SFH(self, tt, zred=None, tage=None): 
            ''' tau or delayed-tau star formation history given parameter values.

            Parameters
            ----------
            tt : 1d or 2d array
                Nparam or NxNparam array specifying the parameter values 

            zred : float, optional
                redshift of the galaxy

            tage : float, optional
                age of the galaxy in Gyrs

            Notes
            -----
            * 12/22/2020: decided to have SFH integrate to 1 by using numerically
               integrated normalization rather than analytic  
            * There are some numerical errors where the SFH integrates to slightly
                greater than one. It should be good enough for most purposes. 
            '''
            if zred is None and tage is None: 
                raise ValueError("specify either the redshift or age of the galaxy")
            if tage is None: 
                tage = self.cosmo.age(zred).value # age in Gyr

            from scipy.special import gammainc
            
            theta       = self._parse_theta(tt) 
            logmstar    = theta['logmstar'] 
            tau         = theta['tau_sfh'] 
            const       = theta['const_sfh']
            sf_start    = theta['sf_start']
            if self._burst: 
                fburst  = theta['fburst'] 
                tburst  = theta['tburst'] 
        
            # tau or delayed-tau 
            power = 1 
            if self._delayed: power = 2

            t = np.linspace(sf_start, np.repeat(tage, sf_start.shape[0]), 100).T 
            tlookback = t - sf_start[:,None]
            dt = np.diff(t, axis=1)[:,0]
            
            tmax = (tage - sf_start) / tau
            normalized_t = (t - sf_start[:,None])/tau[:,None]
            
            # constant contribution 
            sfh = (np.tile(const / (tage-sf_start), (100, 1))).T

            # burst contribution 
            if self._burst: 
                tb = (tburst - sf_start) / tau
                has_burst = (tb > 0)
                fburst[~has_burst] = 0. 
                iburst = np.floor(tb[has_burst] / dt[has_burst] * tau[has_burst]).astype(int)
                dts = (np.tile(dt, (100, 1))).T
                dts[:,0] *= 0.5 
                dts[:,-1] *= 0.5 
                sfh[has_burst, iburst] += fburst[has_burst] / dts[has_burst, iburst]
            else: 
                fburst = 0. 

            # tau contribution 
            ftau = (1. - const - fburst) 
            sfh_tau = (normalized_t **(power - 1) * np.exp(-normalized_t))
            sfh += sfh_tau * (ftau / tau / np.trapz(sfh_tau, normalized_t))[:,None]
            #sfh += ftau[:,None] / tau[:,None] * (normalized_t **(power - 1) *
            #        np.exp(-normalized_t) / gammainc(power, tmax.T)[:,None])
            
            # normalize by stellar mass 
            sfh *= 10**logmstar[:,None]

            if np.atleast_2d(tt).shape[0] == 1: 
                return tlookback[0], sfh[0][::-1]
            else: 
                return tlookback, sfh[:,::-1]

        def ZH(self, tt, zred=None, tage=None, tcosmic=None):
            ''' calculate metallicity history. For `Tau` this is simply a constant
            metallicity value  

            Parameters
            ----------
            tt : 1d or 2d array
                Nparam or [N, Nparam] array specifying the parameter values

            zred : float, optional
                redshift of the galaxy

            tage : float
                age of the galaxy in Gyrs 
            '''
            if zred is None and tage is None: 
                raise ValueError("specify either the redshift or age of the galaxy")
            if tage is None: 
                assert isinstance(zred, float)
                tage = self.cosmo.age(zred).value # age in Gyr

            theta = self._parse_theta(tt)
            Z = theta['metallicity'] 

            if tcosmic is not None: 
                assert tage >= np.max(tcosmic) 
                t = tcosmic.copy() 
            else: 
                t = np.linspace(0, tage, 100)
            
            return t, np.tile(np.atleast_2d(Z).T, (1, 100))

        def _init_model(self, **kwargs): 
            ''' some under the hood initalization of the model and its
            parameterization. 
            '''
            if self._burst: 
                self._parameters = [
                        'logmstar', 
                        'tau_sfh',      # e-folding time in Gyr 0.1 < tau < 10^2
                        'const_sfh',    # constant component 0 < C < 1 
                        'sf_start',     # start time of SFH in Gyr #'sf_trunc'    
                        'fburst',       # fraction of mass formed in an instantaneous burst of star formation
                        'tburst',       # age of the universe when burst occurs 
                        'metallicity', 
                        'dust2']
            else: 
                self._parameters = [
                        'logmstar', 
                        'tau_sfh',      # e-folding time in Gyr 0.1 < tau < 10^2
                        'const_sfh',    # constant component 0 < C < 1 
                        'sf_start',     # start time of SFH in Gyr #'sf_trunc',     
                        'metallicity', 
                        'dust2']

            if not self._emulator: 
                self._sps_model = self._fsps 

            return None 

        def _ssp_initiate(self): 
            ''' initialize sps (FSPS StellarPopulaiton object) 
            '''
            # tau or delayed tau model
            if not self._delayed: sfh = 1 
            else: sfh = 4 

            dust_type   = 2 # Calzetti et al. (2000) attenuation curve
            imf_type    = 1 # chabrier
            self._ssp = fsps.StellarPopulation(
                    zcontinuous=1,          # interpolate metallicities
                    sfh=sfh,                # sfh type 
                    dust_type=dust_type,            
                    imf_type=imf_type)      # chabrier 
            return None  
"""
