import numpy as np
import matplotlib.pyplot as plt
import scipy.special
import time

from .constants import h, c, k_B, R_jup, M_jup, R_sun
from ._atmosphere_solver import AtmosphereSolver

class FluxCalculator:
    def __init__(self, include_condensation=True, method="xsec"):
        '''
        All physical parameters are in SI.

        Parameters
        ----------
        include_condensation : bool
            Whether to use equilibrium abundances that take condensation into
            account.
        num_profile_heights : int
            The number of zones the atmosphere is divided into
        ref_pressure : float
            The planetary radius is defined as the radius at this pressure
        method : string
            "xsec" for opacity sampling, "ktables" for correlated k
        '''
        self.atm = AtmosphereSolver(include_condensation=include_condensation, method=method)

        # scipy.special.expn is slow when called on millions of values, so
        # use interpolator to speed it up
        tau_cache = np.logspace(-6, 3, 1000)
        self.exp3_interpolator = scipy.interpolate.interp1d(
            tau_cache,
            scipy.special.expn(3, tau_cache),
            bounds_error=False,
            fill_value=(0.5, 0))

        
    def change_wavelength_bins(self, bins):        
        '''Same functionality as :func:`~platon.transit_depth_calculator.TransitDepthCalculator.change_wavelength_bins`'''
        self.atm.change_wavelength_bins(bins)


    def _get_binned_fluxes(self, fluxes, n_gauss=10):
        #Step 1: do a first binning if using k-coeffs; first binning is a
        #no-op otherwise
        if self.atm.method == "ktables":
            #Do a first binning based on ktables
            points, weights = scipy.special.roots_legendre(n_gauss)
            percentiles = 100 * (points + 1) / 2
            weights /= 2
            assert(len(depths) % n_gauss == 0)
            num_binned = int(len(depths) / n_gauss)
            intermediate_lambdas = np.zeros(num_binned)
            intermediate_fluxes = np.zeros(num_binned)

            for chunk in range(num_binned):
                start = chunk * n_gauss
                end = (chunk + 1 ) * n_gauss
                intermediate_lambdas[chunk] = np.median(self.atm.lambda_grid[start : end])
                intermediate_fluxes[chunk] = np.sum(fluxes[start : end] * weights)
        elif self.atm.method == "xsec":
            intermediate_lambdas = self.atm.lambda_grid
            intermediate_fluxes = fluxes
        else:
            assert(False)

        
        if self.atm.wavelength_bins is None:
            return intermediate_lambdas, intermediate_fluxes, intermediate_lambdas, intermediate_fluxes
        
        binned_wavelengths = []
        binned_fluxes = []
        for (start, end) in self.atm.wavelength_bins:
            cond = np.logical_and(
                intermediate_lambdas >= start,
                intermediate_lambdas < end)
            binned_wavelengths.append(np.mean(intermediate_lambdas[cond]))
            binned_fluxes.append(np.mean(intermediate_fluxes[cond]))
            
        return intermediate_lambdas, intermediate_fluxes, np.array(binned_wavelengths), np.array(binned_fluxes)

    def _get_photosphere_radii(self, taus, radii):
        intermediate_radii = 0.5 * (radii[0:-1] + radii[1:])
        photosphere_radii = np.array([np.interp(1, t, intermediate_radii) for t in taus])
        return photosphere_radii
    
    def compute_fluxes(self, t_p_profile, planet_mass,
                       planet_radius, dist, logZ=0, CO_ratio=0.53,
                       add_gas_absorption=True, add_H_minus_absorption=False,
                       add_scattering=True, scattering_factor=1,
                       scattering_slope=4, scattering_ref_wavelength=1e-6,
                       add_collisional_absorption=True,
                       cloudtop_pressure=np.inf, custom_abundances=None,
                       T_spot=None, spot_cov_frac=None,
                       ri = None, frac_scale_height=1,number_density=0,
                       part_size=1e-6, part_size_std=0.5, P_quench=1e-99,
                       stellar_blackbody=False,
                       full_output=False):
        '''Most parameters are explained in :func:`~platon.transit_depth_calculator.TransitDepthCalculator.compute_depths`

        Parameters
        ----------
        t_p_profile : Profile
            A Profile object from TP_profile
        '''
        T_profile = t_p_profile.temperatures
        P_profile = t_p_profile.pressures
        #plt.semilogy(T_profile, P_profile)
        #plt.show()
        atm_info = self.atm.compute_params(
            planet_mass, planet_radius, P_profile, T_profile,
            logZ, CO_ratio, add_gas_absorption, add_H_minus_absorption, add_scattering,
            scattering_factor, scattering_slope, scattering_ref_wavelength,
            add_collisional_absorption, cloudtop_pressure, custom_abundances,
            ri, frac_scale_height, number_density, part_size, part_size_std,
            P_quench)

        assert(np.max(atm_info["P_profile"]) <= cloudtop_pressure)
        absorption_coeff = atm_info["absorption_coeff_atm"]
        intermediate_coeff = 0.5 * (absorption_coeff[0:-1] + absorption_coeff[1:])
        intermediate_T = 0.5 * (atm_info["T_profile"][0:-1] + atm_info["T_profile"][1:])
        dr = atm_info["dr"]
        d_taus = intermediate_coeff.T * dr
        taus = np.cumsum(d_taus, axis=1)

        lambda_grid = self.atm.lambda_grid

        reshaped_lambda_grid = lambda_grid.reshape((-1, 1))
        planck_function = 2*h*c**2/reshaped_lambda_grid**5/(np.exp(h*c/reshaped_lambda_grid/k_B/intermediate_T) - 1)

        #padded_taus: ensures 1st layer has 0 optical depth
        padded_taus = np.zeros((taus.shape[0], taus.shape[1] + 1))
        padded_taus[:, 1:] = taus
        integrand = planck_function * np.diff(scipy.special.expn(3, padded_taus), axis=1)
        fluxes = -2 * np.pi * np.sum(integrand, axis=1)
        #print("Flux", np.median(fluxes))
        if not np.isinf(cloudtop_pressure):
            max_taus = np.max(taus, axis=1)
            fluxes_from_cloud = -np.pi * planck_function[:, -1] * (max_taus**2 * scipy.special.expi(-max_taus) + max_taus * np.exp(-max_taus) - np.exp(-max_taus))
            fluxes += fluxes_from_cloud

        fluxes *= (planet_radius / dist)**2
        
        #For correlated k, fluxes has n_gauss points per wavelength, while unbinned_fluxes has 1 point per wavelength        
        unbinned_wavelengths, unbinned_fluxes, binned_wavelengths, binned_fluxes = self._get_binned_fluxes(fluxes)

        if full_output:
            atm_info["planet_spectrum"] = fluxes
            atm_info["unbinned_wavelengths"] = unbinned_wavelengths
            atm_info["unbinned_fluxes"] = unbinned_fluxes
            atm_info["binned_fluxes"] = binned_fluxes
            atm_info["taus"] = taus
            atm_info["contrib"] = -integrand / fluxes[:, np.newaxis]
            atm_info["TP_profile"] = t_p_profile
            return binned_wavelengths, binned_fluxes, atm_info

        return binned_wavelengths, binned_fluxes
