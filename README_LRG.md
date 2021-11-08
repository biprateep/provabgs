# PROVABGS for LRGs: 
Log of modifications:
- Generate FSPS templates for extended redshift range (0.3-1.5)
- There should be a million SEDs for training and 100k for testing. 
- The SEDs are divided into 5 wavelength bins: 1000-2000, 2000-3600, 3600-5500, 5500-7410, 7410-60000. 
- We have to train an emulator for each wavelength bin. This makes it easier on the memory and easier to train.
- BGS only uses the last four bins but LRGs should need all five.
- First we need to train the PCA bases. Use `/bin/pca.py` with the arguments  `name (used: nmf)` `start_fsps_batch (used: 0)` `end_fsps_batch (used: 99)` `num_PCA_bases (used: 50,50,50,50,30)` `wavelength bin (used: 0,1,2,3,4)`
```bash
#example
python pca.py nmf 0 99 50 2
```
# PRObabilistic Value-Added Bright Galaxy Survey (PROVABGS)
[![Gitter](https://badges.gitter.im/provabgs/provabgs.svg)](https://gitter.im/provabgs/provabgs?utm_source=badge&utm_medium=badge&utm_campaign=pr-badge)

`provabgs` is a python package for fitting photometry and spectra from the Dark
Energy Spectroscopic Instrument Bright Galaxy Survey (DESI BGS). 

## Installation
To install the package, clone the github repo and use `pip` to install  
```bash
# clone github repo 
git clone https://github.com/changhoonhahn/provabgs.git

cd provabgs

# install 
pip install -e . 
```

`pip` install coming soon...

### requirements
If you only use the emulators, `provabgs` can run without `fsps`. However, it's
recommended that you install `python-fsps`. See `python-fsps`
[documentation](https://python-fsps.readthedocs.io/en/latest/) for installation
instruction. 

If you're using `provabgs` on NERSC, see [below](#fsps-on-nersc) for 
some notes on installing `FSPS` on `NERSC`.

### fsps on NERSC
I've been running into some issues installing and using `fsps` on NERSC. *e.g.*
there's an import error with libgfotran.so.5. The following may resolve the problem... 
```
module unload PrgEnv-intel
module load PrgEnv-gnu

```

## Team
- ChangHoon Hahn (Princeton)
- Rita Tojeiro (St Andrews)
- Justin Alsing (Stockholm) 
- James Kyubin Kwon (Berkeley) 


## Contact
If you have any questions or need help using the package, please raise a github issue, post a message on gitter, or contact me at changhoon.hahn@princeton.edu
