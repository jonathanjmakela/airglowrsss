# A module for functions used for the conversion of MIGHTI Level 1 files to Level 2.1 and 2.2 files.
# Level 1 files - Calibrated MIGHTI interferograms
# Level 2.1 files - Line-of-sight wind profiles (this is where the onion-peeling inversion happens)
# Level 2.2 files - Vector wind profiles (this is where the A/B matchup happens)
# Altitudes and distances are expressed in km everywhere in the code, except when it's about to be
# saved in a netCDF file in meters.


####################################### VERSION CONTROL ############################################
# These need to be manually changed, when necessary.
# NOTE: When the major version is updated, you should change the History global attribute
# in both the L2.1 and L2.2 netcdf files.
software_version_major = 0 # When this changes, the data version will automatically change as well
software_version_minor = 4 # [0-99], resetting when the major version changes
__version__ = '%i.%02i' % (software_version_major, software_version_minor) # e.g., 2.03
####################################################################################################


############################## GLOBAL PROCESSING PARAMETERS ########################################
# Unless overridden by the user, the following parameters will be used:
global_params = {}

global_params['red'] = {
    'sigma'             : 1.0/630.0304e-9, # reciprocal of center wavelength of emission [m^-1] 
                                           # (Osterbrock et al. 1996)  
    'bin_size'          : 1,              # The number of rows of the interferogram to bin together to 
                                           # improve statistics at the cost of altitude resolution.   
    'account_for_local_projection': True,  # Whether to account for the fact that the line of sight is not
                                           # quite horizontal everywhere along the line of sight
    'integration_order' : 0,               # 0: Use Riemann-sum rule for discretizing line-of-sight integral
                                           # 1: Use trapezoidal rule for discretizing line-of-sight integral
    'top_layer'         : 'exp',           # 'thin': assume VER goes to zero above top layer
                                           # 'exp':  assume VER falls off exponentially in altitude with H=26km
    'sph_asym_thresh'   : 0.19,            # The relative difference in VER estimates from A&B,
                                           # beyond which the spherical asymmetry flag will be
                                           # raised in L2.2.
}

global_params['green'] = {
    'sigma'             : 1.0/557.7338e-9,
    'bin_size'          : 1,
    'account_for_local_projection': True,
    'integration_order' : 0,
    'top_layer'         : 'exp',
    'sph_asym_thresh'   : 0.19,            
}
#####################################################################################################


import numpy as np
import ICON
import bisect
from scipy import integrate
from datetime import datetime, timedelta
import netCDF4
import getpass # for determining who is running the script
import glob
import traceback # for printing detailed error traces


############################################################################################################
##########################################       Level 2.1       ###########################################
############################################################################################################




def phase_to_wind_factor(sigma_opd):
    '''
    Return the value f that satisfies w = f*p, where w is a wind change and p is a phase change.
    dphi = 2*pi*OPD*sigma*v/c (Eq 1 of Englert et al 2007 Appl Opt., and Eq 2 of Harding et al. 2016 SSR)
                               
    INPUTS:
    
      *  sigma_opd   -- TYPE:float, UNITS:none.  sigma times opd: Optical path difference measured 
                                                 in wavelengths. If analyzing an entire row at once, 
                                                 the mean OPD of that row should be used.
                                                 
    OUTPUTS:
    
      *  f     -- TYPE:float, UNITS:m/s/rad. Phase to wind factor, described above.
      
    '''
    c      = 299792458.0 # m/s, speed of light
    return c / (2.*np.pi*sigma_opd)





def unwrap(x, start=0):
    '''
    Unwrap a monotonically increasing phase signal to remove -2*pi jumps.
    This is very similar to np.unwrap, but only unwraps negative jumps. 
    
    INPUTS:
    
      *  x     -- TYPE:array, UNITS:rad. Signal that has -2*pi jumps to remove
      
    OPTIONAL INPUTS:
    
      * start  -- TYPE:int.              The pixel at which to begin unwrapping
      
    OUTPUTS:
    
      *  xnew  -- TYPE:array, UNITS:rad. Copy of x with -2*pi jumps removed
      
    '''
    xnew = np.zeros(len(x))
    xnew[start] = x[start]

    # Go from start forwards
    dx = np.diff(x[start:])
    idx = dx < 0
    dx[idx] = dx[idx] + 2.*np.pi
    xnew[start+1:] = xnew[start] + np.cumsum(dx)

    # Go from start backwards
    dx = np.diff(x[start::-1])
    idx = dx > 0
    dx[idx] = dx[idx] - 2.*np.pi
    xnew[:start] = xnew[start] + np.cumsum(dx)[::-1]
    
    return xnew




def circular_mean(angle0,angle1):
    '''
    Find the mean angle, taking into account 0/360 crossover. For example,
    circular_mean(10,50) is 30, but circular_mean(350,20) is 5.
    
    INPUTS:
    
      *  angle0  -- TYPE:float or array, UNITS:deg. An angle in degrees.
      *  angle1  -- TYPE:float or array, UNITS:deg. An angle in degrees.
      
    OUTPUTS:
    
      *  angle   -- TYPE:float or array, UNITS:deg. The circular mean of the two input angles.
                   
    '''
    x = np.rad2deg(np.angle((np.exp(1j*np.deg2rad(angle0)) + np.exp(1j*np.deg2rad(angle1)))/2.))
    x = np.mod(x,360.)
    return x




def remove_satellite_velocity(I, sat_latlonalt, sat_velocity, sat_velocity_vector, mighti_vectors, sigma_opd,):
    '''
    Modify the interferogram to remove the effect of satellite velocity upon the phase. 
    
    INPUTS:
    
      *  I                   -- TYPE:array(ny,nx),    UNITS:arb.  The MIGHTI interferogram. 
      *  sat_latlonalt       -- TYPE:array(3),        UNITS:(deg,deg,km). Satellite location in WGS84.
      *  sat_velocity        -- TYPE:float,           UNITS:m/s.  ICON velocity.
      *  sat_velocity_vector -- TYPE:array(3),        UNITS:none. The unit ECEF vector describing
                                                                  the direction of ICON's velocity vector.
      *  mighti_vectors      -- TYPE:array(ny,nx,3),  UNITS:none. mighti_vectors[i,j,:] is a unit 3-vector in ECEF
                                                                  coordinates defining the look direction of pixel (i,j).
      *  sigma_opd           -- TYPE:array(nx),       UNITS:none. The optical path difference (measured in wavelengths)
                                                                  for each column of the interferogram.
                                                                  
    OUTPUTS:
    
      *  I                   -- TYPE:array(ny,nx), UNITS:arb.  The MIGHTI interferogram, corrected
                                for the effects of satellite motion on the phase.
                                
    '''
    
    ny,nx = np.shape(I)
    
    # Loop over each pixel, calculating the look direction, projected satellite velocity
    # and the resulting phase shift in the interferogram
    sat_vel_phase = np.zeros((ny,nx))
    for i in range(ny): # loop over rows
        for j in range(nx): # Loop over columns
            look_vector = mighti_vectors[i,j,:]
            proj_sat_vel = sat_velocity * np.dot(sat_velocity_vector, look_vector) # positive apparent wind towards MIGHTI
            # use a different phase-to-wind factor for each column
            sat_vel_phase[i,j] = proj_sat_vel/phase_to_wind_factor(sigma_opd[j])

    # Subtract phase from the interferogram
    I2 = I*np.exp(-1j*sat_vel_phase)
        
    return I2




def bin_array(b, y, lon = False):
    '''
    Downsample y by binning it, improving statistics. Every b
    elements of y will be averaged together to create a new array, y_b, 
    of length ny_b = ceil(len(y)/b). Binning starts at the end of the array, 
    so the first element of y_b may not represent exactly b samples of y.
    
    INPUTS:
    
      *  b    -- TYPE:int,          The number of rows to bin together
      *  y    -- TYPE:array(ny),    The array to be binned
      
    OPTIONAL INPUTS:
    
      *  lon  -- TYPE:bool,         If True, 360-deg discontinuities will
                                    be removed before averaging (e.g., for
                                    longitude binning).
                                    
    OUTPUTS:
    
      *  y_b  -- TYPE:array(ny_b),  The binned array
      
    '''
    # To save time, return quickly if b==1
    if b==1:
        return y
    
    ny = len(y)
    ny_b = int(np.ceil(1.0*ny/b))
    y_b = np.zeros(ny_b, dtype=y.dtype)
    for i in range(0,ny_b): # bin from the end to the beginning.
        i_new   = ny_b-i-1
        i_start = ny-(i+1)*b
        i_stop  = ny-i*b
        
        # grab the samples to be binned
        if np.mod(ny,b)!=0 and i_new==0: # special case in case ny is not divisible by b
            y_samps = y[:i_stop]
        else: # grab 
            y_samps = y[i_start:i_stop]

        if lon:
            y_samps = fix_longitudes(y_samps, 180.)
        y_b[i_new] = np.mean(y_samps)
        
    return y_b
    
    

def bin_uncertainty(b, ye):
    '''
    Determine the uncertainty of a binned array from the uncertainty
    of the un-binned array. Specifically:
    If the array y has uncertainty given by array ye, then the array
    ::
    
      y_b = bin_array(b, y)
        
    has uncertainty given by the array
    ::
    
      ye_b = bin_uncertainty(b, ye)
        
    INPUTS:
    
      *  b    -- TYPE:int,          The number of rows to bin together
      *  ye   -- TYPE:array(ny),    The uncertainty of the pre-binned data
      
    OUTPUTS:
    
      *  ye_b -- TYPE:array(ny_b), The uncertainty of the binned data
      
    '''
    # To save time, return quickly if b==1
    if b==1:
        return ye
    
    ny = len(ye)
    ny_b = int(np.ceil(1.0*ny/b))
    ye_b = np.zeros(ny_b, dtype=ye.dtype)
    for i in range(0,ny_b): # bin from the end to the beginning.
        i_new   = ny_b-i-1
        i_start = ny-(i+1)*b
        i_stop  = ny-i*b
        
        # grab the samples to be binned
        if np.mod(ny,b)!=0 and i_new==0: # special case in case ny is not divisible by b
            ye_samps = ye[:i_stop]
        else: # grab 
            ye_samps = ye[i_start:i_stop]

        ye_b[i_new] = 1.0/len(ye_samps) * np.sqrt(np.sum(ye_samps**2))
        
    return ye_b
    
    
    
    
def bin_image(b, I):
    '''
    Downsample the interferogram in altitude to improve statistics while
    degrading vertical resolution. Every b rows will be averaged together. 
    Binning starts at high altitudes, so the lower rows of I_b may not represent 
    exactly b rows of I.
    
    INPUTS:
    
      *  b           -- TYPE:int,                        The number of rows to bin together
      *  I           -- TYPE:array(ny,nx),   UNITS:arb.  The MIGHTI interferogram
      
    OUTPUTS:
    
      *  I_b         -- TYPE:array(ny_b,nx), UNITS:arb.  The binned MIGHTI interferogram
      
    '''
    
    # To save time, return quickly if b==1
    if b==1:
        return I
    
    ny,nx = np.shape(I)
    # Initial call to bin_array to see what the size of the new image will be
    tmp = bin_array(b, I[:,0])
    ny_b = len(tmp)
    
    # Bin the interfogram column by column
    I_b = np.zeros((ny_b,nx),dtype=I.dtype)
    for i in range(nx):
        I_b[:,i] = bin_array(b,I[:,i])
    return I_b





def create_observation_matrix(tang_alt, icon_alt, top_layer='exp', integration_order=0):
    '''
    Define the matrix D whose inversion is known as "onion-peeling." 
    
    The forward model is:
    ::
    
        I = D * Ip
        
    where I is the measured interferogram, D is the observation matrix, and Ip is the 
    onion-peeled interferogram. If integration_order is 1, the observation matrix is 
    created by assuming the spectrum (and thus the interferogram) is a piecewise linear 
    function of altitude, treating the values of the interferogram at the tangent locations
    as the unknowns, and writing the measurements as a linear function of the unknowns.
    If integration_order is 0, the same recipe is followed, except the spectrum is 
    assumed to be a piecewise constant function of altitude, and the unknowns are the 
    values of the interferogram at the midpoint between two tangent altitudes.
    
    Setting integration_order==0 is better for precision.
    
    Setting integration_order==1 is better for accuracy.
    
    INPUTS:
    
      *  tang_alt   -- TYPE:array(ny),    UNITS:km.   Tangent altitudes of each row of interferogram.
      *  icon_alt   -- TYPE:float,        UNITS:km.   Altitude of the satellite.
      
    OPTIONAL INPUTS:
    
      *  top_layer  -- TYPE:str,          'thin': assume VER goes to zero above top layer
                                          'exp':  assume VER falls off exponentially in altitude (default)
      *  integration_order -- TYPE:int,
      
                      * 0: Use Riemann-sum rule for discretizing line-of-sight integral (default).
                      * 1: Use trapezoidal rule for discretizing line-of-sight integral.
                      
                                          
    OUTPUTS:
    
      *  D          -- TYPE:array(ny,ny), UNITS:km.   Observation matrix. Also called the "path matrix"
                                                      or "distance matrix"
                                                      
    '''
    
    
    H = 26. # km, assumed scale height of VER falloff with altitude (used if top_layer=='exp')
            # This was found by fitting many profiles for which there was significant
            # emission above 300 km. Profiles were generated from Zhang/Shepherd model and
            # from photochemical model fed by IRI/MSIS. (See MIGHTI SSR paper for details on
            # airglow models).
    
    def q(x,rm,r): 
        # antiderivative of (sqrt(x**2 + rm**2) - r)   w.r.t. x
        return 0.5*x*np.sqrt(rm**2 + x**2) + 0.5*rm**2 * np.log(2.*(np.sqrt(rm**2 + x**2)+x)) - r*x
    
    M = len(tang_alt)   # Number of rows of interferogram

    RE = 6371. # km, assume the Earth is locally spherical with an effective radius RE.
               # (The estimated winds are barely sensitive to the choice of RE. This
               #  approximation introduces an error < 1mm/s)
               
    D = np.zeros((M,M))
    
    #################### Zero-order integration #######################
    # Assume airglow is constant within thin altitude shells. This is
    # analogous to Riemann sum integration
    if integration_order == 0:
    
        theta = np.deg2rad(ICON.tang_alt_to_ze(tang_alt, icon_alt, RE))
        
        # Define grid. Bottom of each layer is defined by tangent height of observation.
        rbottom = tang_alt
        # Define top of each layer.
        rtop = rbottom.copy()
        rtop[:-1] = rbottom[1:]
        rtop[-1] = rbottom[-1] + (rtop[1]-rbottom[1])
        # Define midpt of each layer
        rmid = (rbottom + rtop)/2

        # Build observation matrix
        for m in range(M):
            for k in range(M):
                th = theta[m]
                rb = rbottom[k]
                rt = rtop[k]
                sb = np.cos(th)**2 - 1 + ((RE+rb)/(RE+icon_alt))**2
                st = np.cos(th)**2 - 1 + ((RE+rt)/(RE+icon_alt))**2
                if sb < 0: # there is no intersection of LOS with altitude rb. Set term to 0.
                    # Note: this might be due to numerical rounding for tangent altitude. 
                    # Do the same thing either way.
                    sb = 0.
                if st < 0: # there is no intersection of LOS with altitude rt. Set term to 0.
                    st = 0.
                D[m,k] = 2*(RE+icon_alt) * ( np.sqrt(st) - np.sqrt(sb) )
           
            if top_layer == 'exp': # Use exponential falloff model
                rt = tang_alt[m] + RE
                r0 = tang_alt[-1] + RE
                
                def func(x, rt):
                    # The extrapolation function to be numerically integrated. (Eq 6 in Harding et al. 2016 SSR)
                    return np.exp(-1./H*(np.sqrt(x**2 + rt**2) - r0))
                
                x0 = np.sqrt(r0**2- rt**2)
                D[m,M-1] = 2.*integrate.quad(func, x0, np.inf, args=(rt))[0]
                
    #################### First-order integration #######################
    # Assume airglow varies linearly within thin altitude shells. This is
    # analogous to trapezoidal rule integration
    elif integration_order == 1:
        for m in range(M):
            rm   = RE + tang_alt[m]
            # Loop over regions
            for k in range(m,M-1):
                # Region k is between nodes (i.e., tangent altitudes) k and k+1
                rk   = RE + tang_alt[k]
                rkp1 = RE + tang_alt[k+1]
                # Compile the contribution from this region to the nodes below and above, using the
                # analytical evaluation of the Abel integral.
                wkkp1 = 2./(rk-rkp1) * ( q(np.sqrt(rk**2  -rm**2),rm,rk)   - q(np.sqrt(rkp1**2-rm**2),rm,rk  ) )
                wkk   = 2./(rk-rkp1) * ( q(np.sqrt(rkp1**2-rm**2),rm,rkp1) - q(np.sqrt(rk**2  -rm**2),rm,rkp1)  )

                D[m,k] += wkk
                D[m,k+1] += wkkp1
                
            # Handle contributions from above 300km differently, depending on top_layer='thin' or 'exp':
            if top_layer == 'thin': # Use assumption that airglow goes to zero just above top altitude
                # Calculate contribution to top node from above top tangent altitude
                rk   = RE + tang_alt[M-1]
                rkp1 = RE + tang_alt[M-1] + (tang_alt[M-1]-tang_alt[M-2])
                wkk = 2./(rk-rkp1) * ( q(np.sqrt(rkp1**2-rm**2),rm,rkp1) - q(np.sqrt(rk**2  -rm**2),rm,rkp1)  )
                D[m,M-1] += wkk
                
            elif top_layer == 'exp': # Use exponential falloff model
                rt = tang_alt[m] + RE
                r0 = tang_alt[-1] + RE
                
                def func(x, rt):
                    # The extrapolation function to be numerically integrated. (Eq 6 in Harding et al. 2016 SSR)
                    return np.exp(-1./H*(np.sqrt(x**2 + rt**2) - r0))
                
                x0 = np.sqrt(r0**2- rt**2)
                D[m,M-1] += 2.*integrate.quad(func, x0, np.inf, args=(rt))[0]
                
    else:
        raise Exception('"integration_order == %i" not supported. Use 0 or 1.' % integration_order)
    
    return D






def create_local_projection_matrix(tang_alt, icon_alt):
    '''
    Define the matrix B whose entries give the factor by which a horizontal wind
    would be projected onto the line of sight. This has the same shape as the
    observation matrix (i.e., distance matrix). At the tangent point, this factor
    is 1.0. Far from the tangent point, this factor is smaller. If this effect is 
    accounted for, it makes a small change in the winds (less than 5 m/s).
    
    INPUTS:
    
      *  tang_alt   -- TYPE:array(ny),    UNITS:km.   Tangent altitudes of each row of interferogram.
      *  icon_alt   -- TYPE:float,        UNITS:km.   Altitude of the satellite.
      
    OUTPUTS:
    
      *  B          -- TYPE:array(ny,ny), UNITS:km.   Local projection matrix. B[i,j] = cos(angle 
                                                      between ray i and the tangent of shell j
                                                      at the point where they intersect)
                                                      
    '''
    
    # Assume the Earth is locally spherical with an effective radius RE.
    # (The estimated winds are barely sensitive to the choice of RE. This
    #  approximation introduces an error < 1mm/s)
    RE = 6371.
    theta = ICON.tang_alt_to_ze(tang_alt, icon_alt, RE)
    
    ny = len(tang_alt)
    
    # Calculate local-horizontal projection factors
    B = np.nan*np.zeros((ny,ny)) # matrix to hold cosine correction factors
    for i in range(ny):
        for j in range(i,ny): # only calculate upper triangular part
            th = theta[i]
            r = tang_alt[j]
            B[i,j] = (RE+icon_alt)/(RE+r) * np.sin(np.deg2rad(th))
    return B
     

    
    
    
def extract_phase_from_row(row, unwrapping_column):
    '''
    Given a 1-D interference pattern (i.e., a row of the intererogram), 
    analyze it to get a single phase value, which represents the wind.
    
    INPUTS:
    
      *  row               -- TYPE:array(nx), UNITS:arb.   A row of the complex-valued, MIGHTI interferogram.
      *  unwrapping_column -- TYPE:int.                    The column at which to begin unwrapping.
      
    OUTPUTS:
    
      *  phase             -- TYPE:float,     UNITS:rad. 
      
    '''
    
    row_phase = np.angle(row)

    # Average phase and then take delta. Need unwrapping for this.
    phaseu = unwrap(row_phase, unwrapping_column)
    meanphase = np.mean(phaseu)
    return meanphase

    
    
    
    
def perform_inversion(I, tang_alt, icon_alt, I_phase_uncertainty, I_amp_uncertainty, unwrapping_column,
                      top_layer='exp', integration_order=0, account_for_local_projection=True):
    '''
    Perform the onion-peeling inversion on the interferogram to return
    a new interferogram, whose rows refer to specific altitudes. In effect,
    this function undoes the integration along the line of sight.
    
    INPUTS:
    
      *  I           -- TYPE:array(ny,nx), UNITS:arb.  The complex-valued, MIGHTI interferogram.
      *  tang_alt    -- TYPE:array(ny),    UNITS:km.   Tangent altitudes of each row of interferogram.
      *  icon_alt    -- TYPE:float,        UNITS:km.   Altitude of the satellite.
      *  I_phase_uncertainty -- TYPE:array(ny), UNITS:rad. Uncertainty in the unwrapped, mean phase of each row of I.
                                                           This is provided in L1 file.
      *  I_amp_uncertainty   -- TYPE:array(ny), UNITS:arb. Uncertainty in the summed amplitude of each row of I.
                                                           This is provided in L1 file.
      *  unwrapping_column -- TYPE:int.                 The column at which to begin unwrapping the phase.
      
    OPTIONAL INPUTS:
    
      *  top_layer   -- TYPE:str,          'thin': assume VER goes to zero above top layer
                                           'exp':  assume VER falls off exponentially in altitude (default)
      *  integration_order -- TYPE:int,
      
                      * 0: Use Riemann-sum rule for discretizing line-of-sight integral (default).
                      * 1: Use trapezoidal rule for discretizing line-of-sight integral.
                      
      *  account_for_local_projection   -- TYPE:bool.   If False, a simple inversion is used.
                                           If True, the inversion accounts for the fact that the ray is not 
                                           perfectly tangent to each shell at each point along the ray. 
                                           (default True)

    OUTPUTS:
    
      *  Ip                -- TYPE:array(ny,nx), UNITS:arb. The complex-valued, onion-peeled interferogram.
      *  phase             -- TYPE:array(ny),    UNITS:rad. The unwrapped, mean phase of each row of Ip.
      *  amp               -- TYPE:array(ny),    UNITS:arb. The amplitude of each row of Ip.
      *  phase_uncertainty -- TYPE:array(ny),    UNITS:rad. The uncertainty of phase
      *  amp_uncertainty   -- TYPE:array(ny),    UNITS:rad. The uncertainty of amp
      
    '''
    
    if top_layer not in ['exp','thin']:
        raise ValueError('Argument top_layer=\'%s\' not recognized. Use \'exp\' or \'thin\'.' % top_layer)
    if integration_order not in [0,1]:
        raise ValueError('Argument integration_order=\'%s\' not recognized. Use 0 or 1')
        
        
    ny,nx = np.shape(I)
    
    # Create the path matrix
    D = create_observation_matrix(tang_alt, icon_alt, top_layer=top_layer, integration_order=integration_order)
    
    
    
    ######### Onion-peeling inversion and amp/phase extraction #########
    # The inversion will proceed in different ways depending on whether
    # we will try to account for the local horizontal projection.
    phase = np.zeros(ny) # phases at each altitude
    if not account_for_local_projection:
        
        # This is implemented with a simple linear inversion
        Ip = np.linalg.solve(D,I)
        for i in range(ny):
            phase[i] = extract_phase_from_row(Ip[i,:], unwrapping_column)
        
    else:
        # The problem becomes nonlinear, but still solvable in closed form.
        # This code implements Eq (9) in the MIGHTI L2 Space Science Reviews
        # paper (Harding et al. 2016).
        
        B = create_local_projection_matrix(tang_alt, icon_alt)
        Ip = np.zeros((ny,nx), dtype=complex) # onion-peeled interferogram

        for i in range(ny)[::-1]: # onion-peel from the top altitude down
            dii = D[i,i] # path length
            Li = I[i,:] # we will peel off the other layers from this row
            # Loop over layers above this one
            for j in range(i+1,ny):
                dij = D[i,j]
                # Calculate the normalized jth row without the wind component
                Ij = Ip[j,:]*np.exp(-1j*phase[j])
                # Calculate it with the projected wind component
                Ij_proj = Ij*np.exp(1j*phase[j]*B[i,j])
                # Remove this contribution from the current layer
                Li = Li - dij*Ij_proj
            # final normalization by this layer's path length
            Li = Li/dii
            Ip[i,:] = Li
            # Analyze the layer to get the phase, and store it.
            phase[i] = extract_phase_from_row(Li,  unwrapping_column)
            
    amp = np.sum(abs(Ip),axis=1)        
    


    ######### Uncertainty propagation #########
    # Uncertainties can be propagated using simple linear inversion formula
    # (i.e., as if account_for_local_projection=False) to a very good approximation
    # (less than 1% error).
    
    ### Step 0: Characterize L1 and L2.1 interferograms with a single amp/phase per row
    ph_L1 = np.zeros(ny)
    for i in range(ny):
        ph_L1[i] = extract_phase_from_row(I[i,:], unwrapping_column)
    A_L1 = np.sum(abs(I),axis=1)
    ph_L2 = phase.copy() # this was calculated above
    A_L2 = amp.copy() # this was calculated above
    # If amp is exactly zero (unlikely in practice), then replace it with a small number
    # so that uncertainties can be calculated.
    A_L2[A_L2==0.0] = 1e-6
        
    ### Step 1: Transform amp/phase uncertainties to real/imag uncertainties
    # Each row will have a 2x2 covariance matrix describing the real and imaginary parts
    cov_real_imag_L1 = np.zeros((ny,2,2))
    for m in range(ny):
        # Jacobian of transformation from ampl/phase to real/imag.
        J = np.array([[np.cos(ph_L1[m]), -A_L1[m]*np.sin(ph_L1[m])],
                      [np.sin(ph_L1[m]),  A_L1[m]*np.cos(ph_L1[m])]])
        cov_amp_phase = np.diag([I_amp_uncertainty[m], I_phase_uncertainty[m]])**2 # assuming uncorrelated
        cov_real_imag_L1[m,:,:] = J.dot(cov_amp_phase).dot(J.T) # Error propagation

    ### Step 2: Propagate uncertainties through the path length inversion
    # Treat real and imaginary parts separately.
    # Build covariance matrix of vector of real parts and of imag parts.
    cov_real_L1 = np.diag(cov_real_imag_L1[:,0,0]) # assume rows are uncorrelated
    cov_imag_L1 = np.diag(cov_real_imag_L1[:,1,1]) # assume rows are uncorrelated
    # Standard uncertainty propagation for matrix multiplication
    Dinv = np.linalg.inv(D)
    cov_real_L2 = Dinv.dot(cov_real_L1).dot(Dinv.T)
    cov_imag_L2 = Dinv.dot(cov_imag_L1).dot(Dinv.T)
    sigma_real_L2 = np.sqrt(np.diag(cov_real_L2))
    sigma_imag_L2 = np.sqrt(np.diag(cov_imag_L2))
    
    ### Step 3: Transform back to amp/phase #########
    # Each row will have a 2x2 covariance matrix describing the amplitude and phase
    cov_amp_phase_L2 = np.zeros((ny,2,2))
    for m in range(ny):
        # Jacobian of transformation from ampl/phase to real/imag
        J = np.array([[np.cos(ph_L2[m]), -A_L2[m]*np.sin(ph_L2[m])],
                      [np.sin(ph_L2[m]),  A_L2[m]*np.cos(ph_L2[m])]])
        # Jacobian of transformation from real/imag to ampl/phase
        Jinv = np.linalg.inv(J)
        cov_real_imag = np.diag([sigma_real_L2[m], sigma_imag_L2[m]])**2 # assume uncorrelated
        cov_amp_phase_L2[m,:,:] = Jinv.dot(cov_real_imag).dot(Jinv.T)
    # Extract amplitude and phase uncertainties
    amp_uncertainty = np.sqrt(cov_amp_phase_L2[:,0,0])
    phase_uncertainty = np.sqrt(cov_amp_phase_L2[:,1,1])
            
    return Ip, phase, amp, phase_uncertainty, amp_uncertainty





def fix_longitudes(lons, lon_target):
    '''
    Unwrap the list of longitudes to avoid 360-deg jumps. The list will
    be fixed so that it contains a value within 180 deg of lon_target and
    is otherwise continuous.
    
    INPUTS:
    
      *  lons       -- TYPE:array, UNITS:deg. An ordered list of longitudes to be unwrapped.
      *  lon_target -- TYPE:float, UNITS:deg. See above.
      
    OUTPUTS:
    
      *  lons_new   -- TYPE:array, UNITS:deg. An ordered list of longitudes with jumps removed.
      
    '''
    lons_new = np.array(lons).copy()
    
    # Find the index with value closest to lon_target (mod 360)
    diff_vec = np.mod(lons_new - lon_target + 180., 360.) - 180. 
    k = np.argmin(abs(diff_vec))
    # Change the entire array up or down by 360 (or a multiple) if necessary, keying off of target_lon.
    n = round((lons_new[k] - lon_target)/360.)
    lons_new = lons_new - n*360.
        
    # Define function to remove jumps
    def fix_jump(jump, val):
        n = round(jump/360.)
        return val - n*360. 
    # Traverse right, removing jumps > +/- 180
    for i in range(k+1,len(lons_new)):
        jump = lons_new[i] - lons_new[i-1]
        lons_new[i] = fix_jump(jump, lons_new[i])
    # Traverse left, removing jumps > +/- 180
    for i in range(k-1,-1,-1):
        jump = lons_new[i] - lons_new[i+1]
        lons_new[i] = fix_jump(jump, lons_new[i])   

    return lons_new





def attribute_measurement_location(tang_lat, tang_lon, tang_alt, integration_order=0):
    '''
    Determine the geographical location to which the measurement will be attributed. Depending
    on integration_order (see function create_observation_matrix), this will either return
    the tangent locations, or the midpoint between two adjacent tangent locations.

    INPUTS:
    
      *  tang_lat    -- TYPE:array(ny), UNITS:deg.   Tangent latitudes.
      *  tang_lon    -- TYPE:array(ny), UNITS:deg.   Tangent longitudes.
      *  tang_alt    -- TYPE:array(ny), UNITS:km.    Tangent altitudes.
      
    OPTIONAL INPUTS:
    
      *  integration_order -- TYPE:int   
      
                      * 0: Use Riemann-sum rule for discretizing line-of-sight integral (default).
                      * 1: Use trapezoidal rule for discretizing line-of-sight integral.
                      
    OUTPUTS:
    
      *  lat         -- TYPE:array(ny), UNITS:deg.   Measurement latitudes.
      *  lon         -- TYPE:array(ny), UNITS:deg.   Measurement longitudes.
      *  alt         -- TYPE:array(ny), UNITS:km.    Measurement altitudes.
      
    '''
    if integration_order not in [0,1]:
        raise ValueError('integration_order = "%s" not recognized. Use 0 or 1')
    
    def shift_up_by_half(vec):
        """
        Shift the input vector up by half the resolution. Extrapolate for the top entry.
        """
        bottom = vec
        top = bottom.copy()
        top[:-1] = top[1:]
        top[-1] = top[-1] + (top[-2] - bottom[-2])
        return 0.5 * top + 0.5 * bottom

    def shift_up_by_half_angle(vec):
        """
        Shift the input vector up by half the resolution. Extrapolate for the top entry.
        Use circular mean instead of arithmetic mean. This is intended for longitude
        calculations.
        """
        vec_new = fix_longitudes(vec, vec[0])
        bottom = vec
        top = bottom.copy()
        top[:-1] = top[1:]
        top[-1] = top[-1] + (top[-2] - bottom[-2])
        mid = np.zeros(len(bottom))
        for i in range(len(mid)):
            mid[i] = circular_mean(top[i], bottom[i])

        return mid
    
    if integration_order == 1:
        lat = tang_lat
        lon = tang_lon
        alt = tang_alt
    else:
        lat = shift_up_by_half(tang_lat)
        lon = shift_up_by_half_angle(tang_lon)
        alt = shift_up_by_half(tang_alt)
        
    return lat, lon, alt





def los_az_angle(sat_latlonalt, lat, lon, alt):
    '''
    Calculate the azimuth angle of the line of sight, evaluated at the 
    measurement location (lat, lon, alt). Assumes WGS84 Earth.
    
    INPUTS:
    
      *  sat_latlonalt -- TYPE:array(3),  UNITS:(deg,deg,km). Satellite location in WGS84.
      *  lat           -- TYPE:array(ny), UNITS:deg.          Measurement latitudes.
      *  lon           -- TYPE:array(ny), UNITS:deg.          Measurement longitudes.
      *  alt           -- TYPE:array(ny), UNITS:km.           Measurement altitudes.
      
    OUTPUTS:
    
      *  az            -- TYPE:array(ny), UNITS:deg.          Azimuth angle of line of sight
                          from the satellite to the measurement location, evaluated at the 
                          measurement location. Degrees East of North.
                          
    '''
    ny = len(lat)
    local_az = np.zeros(ny)
    sat_xyz = ICON.wgs84_to_ecef(sat_latlonalt)
    for i in range(ny):
        meas_latlonalt = np.array([lat[i], lon[i], alt[i]]) # where the measurement is attributed to
        meas_xyz = ICON.wgs84_to_ecef(meas_latlonalt)
        look_xyz = meas_xyz - sat_xyz # look direction
        loc_az, loc_ze = ICON.ecef_to_azze(meas_latlonalt, look_xyz) # look direction in az, ze at measurement point.
        local_az[i] = loc_az  
    return local_az





def remove_Earth_rotation(v_inertial, az, lat, lon, alt):
    '''
    Transform wind measurement from inertial coordinates to a reference
    frame rotating with the Earth. This can be thought of as "removing 
    Earth rotation from the line-of-sight measurement."
    
    INPUTS:
    
      *  v_inertial    -- TYPE:array(ny), UNITS:m/s.   Line-of-sight velocity in inertial
                          coordinates, positive towards MIGHTI.
      *  az            -- TYPE:array(ny), UNITS:deg.   Azimuth angle of line of sight
                          from the satellite to the measurement location, evaluated at the 
                          measurement location. Degrees East of North. See los_az_angle() above.
      *  lat           -- TYPE:array(ny), UNITS:deg.   Measurement latitudes.
      *  lon           -- TYPE:array(ny), UNITS:deg.   Measurement longitudes.
      *  alt           -- TYPE:array(ny), UNITS:km.    Measurement altitudes.
      
    OUTPUTS:
    
      *  v             -- TYPE:array(ny), UNITS:m/s.   Line-of-sight velocity in Earth-fixed
                          coordinates, positive towards MIGHTI.
                          
    '''
    ny = len(v_inertial)
    corot_contribution = np.zeros(ny)
    for i in range(ny):
        meas_latlonalt = np.array([lat[i], lon[i], alt[i]]) # where the measurement is attributed to
        meas_xyz = ICON.wgs84_to_ecef(meas_latlonalt)
        rho = np.sqrt(meas_xyz[0]**2 + meas_xyz[1]**2)
        sidereal_day_length = 23.*60.*60. + 56.*60. + 4. # sidereal day is 23 hrs 56 min 4 sec 
        corot_vel = 2.*np.pi*rho/sidereal_day_length*1e3
        # Compute component along LoS
        corot_contribution[i] = -corot_vel * np.sin(np.deg2rad(az[i])) # positive towards MIGHTI
    v = v_inertial - corot_contribution
    return v






def interpolate_linear(x, y, x0, extrapolation='hold', prop_err = False, yerr = None):
    '''
    Linear interpolation of the function y = f(x) to the location x0.
    x and y are vectors comprising samples of this function. There is also
    an option to propagate errors to the interpolated value. This function is
    5 times faster than scipy.interpolate.interp1d, and allows for
    zero-order-hold extrapolation. If you are interpolating to many points, 
    then scipy.interpolate.interp1d is likely faster. 

    INPUTS:
    
      *  x     -- TYPE:array(n), UNITS:arb. Independent variable of samples of function.
      *  y     -- TYPE:array(n), UNITS:arb. Dependent variable of samples of function.
      *  x0    -- TYPE:float,    UNITS:arb. Independent variable of interpolation point.
      
    OPTIONAL INPUTS:
    
      *  extrapolation -- TYPE:str,        'hold': extrapolate by using values at end points (default)
                                           'none': do not extrapolate. Points will be np.nan
      *  prop_err      -- TYPE:bool,
      
                                      * True:  propagate errors from original to interpolated
                                               value, and return an extra output; yerr must
                                               be specified as an input. 
                                      * False: do not propagate errors, and return only one
                                               output (default).
                                               
      *  yerr          -- TYPE:array(n), UNITS:arb. Error in y, to be propagated to interpolated value.
      
    OUTPUTS:
    
      *  y0    -- TYPE:float,    UNITS:arb. Interpolated value.
      
    OPTIONAL OUTPUT (if prop_err = True):
    
      *  y0err -- TYPE:float,    UNTIS:arb. Propagated error of y0.
      
    '''
    
    if prop_err and yerr is None:
        raise Exception('If prop_err=True, then yerr must be specified')    
        
    # Special corner case: x0 is exactly on the last grid point
    if x0==x[-1]:
        if prop_err:
            return y[-1], yerr[-1]
        else:
            return y[-1]
    
    j0 = bisect.bisect(x,x0) - 1 # index to the left
    j1 = j0 + 1 # index to the right
    y0err = np.nan
    # Handle extrapolations
    if j0 == -1:
        if extrapolation=='hold':
            y0 = y[0]
            if prop_err:
                y0err = yerr[0]
        elif extrapolation == 'none':
            y0 = np.nan
        else: 
            raise Exception('"%s" not understood' % extrapolation)
    elif j1 == len(x):
        if extrapolation=='hold':
            y0 = y[-1]
            if prop_err:
                y0err = yerr[-1]
        elif extrapolation == 'none':
            y0 = np.nan
        else: 
            raise Exception('"%s" not understood' % extrapolation)
    else: # linear interpolation
        w1 = (x0-x[j0]) / (x[j1]-x[j0]) # weight of y[j1]
        w0 = 1.0-w1 # weight of y[j0]
        y0 = w0*y[j0] + w1*y[j1]
        if prop_err:
            # What is the best way to interpolate errors? 
            # Statistically correct way, but yields counterintuitive results, such as
            # a higher error near the sample points than between them:
            #y0err = np.sqrt(w0**2*yerr[j0]**2 + w1**2*yerr[j1]**2)
            # Simple way: just interpolate errors
            y0err = w0*yerr[j0] + w1*yerr[j1]
    if prop_err:
        return y0, y0err
    else:
        return y0





def level1_to_dict(L1_fn, emission_color):
    '''
    Read a level 1 file and translate it into a dictionary that the 
    level 2.1 processing can use.
    
    INPUTS:
    
      *  L1_fn          -- TYPE:str.  The full path and filename of the level 1 file.
      *  emission_color -- TYPE:str, 'green' or 'red'.
        
    OUTPUTS:
    
      *  L1_dict -- TYPE:dict. A dictionary containing information needed for
                               the level 2.1 processing. See documentation for 
                               level1_dict_to_level21(...) for required keys.
                               

    '''
    
    f = netCDF4.Dataset(L1_fn)
    
    # Is this A or B? There's no variable that says it (yet?) so we have to infer it from the file name
    sensor = None
    if 'MIGHTI-A' in L1_fn:
        sensor = 'A'
    elif 'MIGHTI-B' in L1_fn:
        sensor = 'B'
    else:
        raise Exception('Cannot determine sensor (A or B) from %s' % L1_fn)
    
    L1_dict = {}
    L1_dict['L1_fn']                       = L1_fn
    L1_dict['sensor']                      = sensor
    L1_dict['I_amp']                       = f['ICON_L1_MIGHTI_%s_%s_ENVELOPE' % (sensor, emission_color.upper())][0,:,:]
    L1_dict['I_phase']                     = f['ICON_L1_MIGHTI_%s_%s_PHASE' % (sensor, emission_color.upper())][0,:,:]
    L1_dict['I_amp_uncertainty']           = f['ICON_L1_MIGHTI_%s_%s_ENVELOPE_UNCERTAINTIES' % (sensor, emission_color.upper())][0,:]
    L1_dict['I_phase_uncertainty']         = f['ICON_L1_MIGHTI_%s_%s_PHASE_UNCERTAINTIES' % (sensor, emission_color.upper())][0,:]
    # For tangent locations, only use the center, not the full horizontal distribution
    ny,nx = np.shape(L1_dict['I_amp'])
    tang_lla                               = f['ICON_L1_MIGHTI_%s_%s_TANGENT_LATLONALT' % (sensor, emission_color.upper())][0,:,:,:,nx/2] 
    L1_dict['tang_alt_start']              = tang_lla[0,2,:]
    L1_dict['tang_alt_stop']               = tang_lla[2,2,:]
    L1_dict['tang_lat_start']              = tang_lla[0,0,:]
    L1_dict['tang_lat_stop']               = tang_lla[2,0,:]
    L1_dict['tang_lon_start']              = tang_lla[0,1,:]
    L1_dict['tang_lon_stop']               = tang_lla[2,1,:]
    L1_dict['emission_color']              = emission_color
    # In the L1 file, the ECEF vectors are stored in multidimensional array: (time, start/mid/stop, vector_xyz, vert, horz)
    tmp                                    = f['ICON_L1_MIGHTI_%s_%s_ECEF_UNIT_VECTORS'% (sensor, emission_color.upper())][0,:,:,:,:]
    L1_dict['mighti_ecef_vectors_start']   = np.transpose(tmp[0,:,:,:], (1,2,0)) # V x H x vector
    L1_dict['mighti_ecef_vectors_stop']    = np.transpose(tmp[2,:,:,:], (1,2,0)) # V x H x vector
    icon_vel_vec_start                     = f['ICON_L1_MIGHTI_%s_SC_VELOCITY_ECEF'% sensor][0,0,:]
    icon_vel_vec_stop                      = f['ICON_L1_MIGHTI_%s_SC_VELOCITY_ECEF'% sensor][0,2,:]
    L1_dict['icon_ecef_ram_vector_start']  = icon_vel_vec_start/np.linalg.norm(icon_vel_vec_start)
    L1_dict['icon_ecef_ram_vector_stop']   = icon_vel_vec_stop/np.linalg.norm(icon_vel_vec_stop)
    L1_dict['icon_velocity_start']         = np.linalg.norm(icon_vel_vec_start)
    L1_dict['icon_velocity_stop']          = np.linalg.norm(icon_vel_vec_stop)
    L1_dict['source_files']                = [f.Parents]
    tsec_start                             = f['ICON_L1_MIGHTI_%s_IMAGE_TIMES'% sensor][0,0]*1e-3
    tsec_stop                              = f['ICON_L1_MIGHTI_%s_IMAGE_TIMES'% sensor][0,2]*1e-3
    L1_dict['time_start']                  = datetime(1970,1,1) + timedelta(seconds=tsec_start)
    L1_dict['time_stop']                   = datetime(1970,1,1) + timedelta(seconds=tsec_stop)
    L1_dict['exp_time']                    = tsec_stop - tsec_start
    L1_dict['optical_path_difference']     = f['ICON_L1_MIGHTI_%s_%s_ARRAY_OPD' % (sensor, emission_color.upper())][0,:]*1e-2 # convert to m
    icon_ecef                              = f['ICON_L1_MIGHTI_%s_SC_POSITION_ECEF'% sensor][:][0,:,:] # timetable x [x,y,z]
    icon_latlonalt = np.zeros((3,3))
    for i in range(3):
        icon_latlonalt[i,:] = ICON.ecef_to_wgs84(icon_ecef[i,:])
    L1_dict['icon_alt_start'] = icon_latlonalt[0,2]
    L1_dict['icon_alt_stop']  = icon_latlonalt[2,2]
    L1_dict['icon_lat_start'] = icon_latlonalt[0,0]
    L1_dict['icon_lat_stop']  = icon_latlonalt[2,0]
    L1_dict['icon_lon_start'] = icon_latlonalt[0,1]
    L1_dict['icon_lon_stop']  = icon_latlonalt[2,1]
    L1_dict['unwrapping_column'] = int(f.getncattr('Reference_Pixel_%s' % emission_color.capitalize()))
    
    # Dummy placeholder code for reading global attributes, if that matters
    nc_attrs = f.ncattrs()
    
    
    f.close()
    
    return L1_dict



    


def level1_dict_to_level21_dict(L1_dict, sigma = None, top_layer = None, 
                                integration_order = None, account_for_local_projection = None, 
                                bin_size = None):
    '''
    High-level function to run the Level 2.1 processing. It takes a dictionary (containing
    input variables extracted from a Level 1 file) and outputs a dictionary (containing 
    output variables, which can be written to a file using save_nc_level21).
    
    INPUTS:
    
      *  L1_dict       -- TYPE:dict.  A dictionary containing variables needed for
                                      the level 2.1 processing:
                                             
                                      * L1_fn                      -- TYPE:str.                      
                                                                      Level 1 filename
                                      * sensor                     -- TYPE:str.
                                                                      Which sensor took the data: 'A' or 'B'
                                      * I_amp                      -- TYPE:array(ny,nx), UNITS:arb.  
                                                                      Magnitude of interferogram
                                      * I_phase                    -- TYPE:array(ny,nx), UNITS:rad.  
                                                                      Phase of interferogram
                                      * I_amp_uncertainty          -- TYPE:array(ny),    UNITS:arb.  
                                                                      Uncertainty in the sum of each row of I_amp
                                      * I_phase_uncertainty        -- TYPE:array(ny),    UNITS:rad. 
                                                                      Uncertainty in the mean phase of each row of interferogram
                                      * tang_alt_start             -- TYPE:array(ny),    UNITS:km.   
                                                                      Tangent altitudes at beginning of exposure
                                      * tang_alt_stop              -- TYPE:array(ny),    UNITS:km.   
                                                                      Tangent altitudes at end of exposure
                                      * tang_lat_start             -- TYPE:array(ny),    UNITS:deg.  
                                                                      Tangent latitudes at beginning of exposure
                                      * tang_lat_stop              -- TYPE:array(ny),    UNITS:deg.  
                                                                      Tangent latitudes at end of exposure
                                      * tang_lon_start             -- TYPE:array(ny),    UNITS:deg.  
                                                                      Tangent longitudes at beginning of exposure
                                      * tang_lon_stop              -- TYPE:array(ny),    UNITS:deg.  
                                                                      Tangent longitudes at end of exposure
                                      * emission_color             -- TYPE:str.                    
                                                                      'red' or 'green'
                                      * icon_alt_start             -- TYPE:float,        UNITS:km.   
                                                                      Spacecraft altitude at beginning of exposure
                                      * icon_alt_stop              -- TYPE:float,        UNITS:km.   
                                                                      Spacecraft altitude at end of exposure
                                      * icon_lat_start             -- TYPE:float,        UNITS:deg.  
                                                                      Spacecraft latitude at beginning of exposure
                                      * icon_lat_stop              -- TYPE:float,        UNITS:deg.  
                                                                      Spacecraft latitude at end of exposure
                                      * icon_lon_start             -- TYPE:float,        UNITS:deg.  
                                                                      Spacecraft longitude at beginning of exposure
                                      * icon_lon_stop              -- TYPE:float,        UNITS:deg.  
                                                                       Spacecraft longitude at end of exposure
                                      * mighti_ecef_vectors_start  -- TYPE:array(ny,nx,3).           
                                                                      Unit ECEF vector of line of sight of each pixel at beginning of exposure 
                                      * mighti_ecef_vectors_stop   -- TYPE:array(ny,nx,3).           
                                                                      Unit ECEF vector of line of sight of each pixel at end of exposure 
                                      * icon_ecef_ram_vector_start -- TYPE:array(3).                 
                                                                      Unit ECEF vector of spacecraft ram at beginning of exposure
                                      * icon_ecef_ram_vector_stop  -- TYPE:array(3).                 
                                                                      Unit ECEF vector of spacecraft ram at end of exposure
                                      * icon_velocity_start        -- TYPE:float.        UNITS:m/s.  
                                                                      Spacecraft velocity at beginning of exposure (TODO: inertial or EF velocity?)
                                      * icon_velocity_stop         -- TYPE:float.        UNITS:m/s.  
                                                                      Spacecraft velocity at end of exposure
                                      * source_files               -- TYPE:list of strs.             
                                                                      All files that were used to generate this L1 file
                                      * time_start                 -- TYPE:datetime (timezone naive).                
                                                                      Start of exposure in UTC
                                      * time_stop                  -- TYPE:datetime (timezone naive).                 
                                                                      End of exposure in UTC
                                      * exp_time                   -- TYPE:float.        UNITS:s.    
                                                                      Length of exposure
                                      * optical_path_difference    -- TYPE:array(nx).    UNITS:m.    
                                                                      Optical path difference for each column of interferogram
                                      * unwrapping_column          -- TYPE:int.           
                                                                      The column at which to begin unwrapping the phase of the interferogram.
                                                                      This is necessary to ensure that the zero wind phase (which was removed
                                                                      in the L1 processing) does not have a 2pi ambiguity.
                                             
    OPTIONAL INPUTS - If None, defaults from MIGHTI_L2.global_params will be used 
    
      *  sigma               -- TYPE:float, UNITS:m^-1. The wavenumber of the emission (1/wavelength)
      *  top_layer           -- TYPE:str, 'thin': assume VER goes to zero above top layer
                                          'exp':  assume VER falls off exponentially in altitude
      *  integration_order   -- TYPE:int, 0: Use Riemann-sum rule for discretizing line-of-sight integral
                                          1: Use trapezoidal rule for discretizing line-of-sight integral
      *  account_for_local_projection -- TYPE:bool. If False, a simple inversion is used.
                                         If True, the inversion accounts for the fact that the ray is not 
                                         perfectly tangent to each shell at each point along the ray.
      *  bin_size            -- TYPE:int, The number of rows of the interferogram to bin together to 
                                          improve statistics at the cost of altitude resolution.
                                                                          
    OUTPUTS:
    
      *  L21_dict            -- TYPE:dict. A dictionary containing output variables of the Level 2.1 processing:

                                * los_wind                  -- TYPE:array(ny),   UNITS:m/s.   Line-of-sight wind profile 
                                * los_wind_error            -- TYPE:array(ny),   UNITS:m/s.   Uncertainty of los_wind (1-sigma)
                                * lat                       -- TYPE:array(ny),   UNITS:deg.   Latitude of each point in profile
                                * lon                       -- TYPE:array(ny),   UNITS:deg.   Longitude of each point in profile
                                * alt                       -- TYPE:array(ny),   UNITS:alt.   Altitude of each point in profile
                                * time_start                -- TYPE:datetime (timezone naive) Time at start of exposure in UTC
                                * time_stop                 -- TYPE:datetime (timezone naive) Time at end of exposure in UTC
                                * exp_time                  -- TYPE:float,       UNITS:s.     Exposure time
                                * az                        -- TYPE:array(ny),   UNITS:deg.   The azimuth angle of the line of sight
                                                                                              at the tangent point (deg East of North)
                                * emission_color            -- TYPE:str.                      'red' or 'green'
                                * sensor                    -- TYPE:str.                      'A' or 'B'
                                * resolution_along_track    -- TYPE:array(ny),   UNITS:km.    Horizontal resolution along the line of sight
                                * resolution_cross_track    -- TYPE:array(ny),   UNITS:km.    Horizontal resolution perpendicular to line of sight
                                * resolution_alt            -- TYPE:array(ny),   UNITS:km.    Vertical resolution
                                * icon_alt                  -- TYPE:float,       UNITS:km.    Spacecraft altitude
                                * icon_lat                  -- TYPE:float,       UNITS:deg.   Spacecraft latitude
                                * icon_lon                  -- TYPE:float,       UNITS:deg.   Spacecraft longitude [0,360]
                                * fringe_amplitude          -- TYPE:array(ny),   UNITS:arb.   The fringe contrast, a proxy for volume emission rate
                                * fringe_amplitude_error    -- TYPE:array(ny),   UNITS:arb.   Uncertainty in fringe_amplitude (1-sigma)
                                * mighti_ecef_vectors       -- TYPE:array(ny,3).              ECEF unit vector for each line of sight
                                * icon_velocity_ecef_vector -- TYPE:array(3).    UNITS:m/s.   ECEF vector of spacecraft velocity (TODO: inertial or EF vel?) 
                                * file_creation_time        -- TYPE:datetime (timezone naive) Time this processing was run in UTC
                                * source_files              -- TYPE:list of str.              All science files that went into creating this file
                                * bin_size                  -- TYPE:int.                      Bin size used in the processing
                                * top_layer                 -- TYPE:str.                      How the top layer was handled: 'thin' or 'exp'
                                * integration_order         -- TYPE:int.                      Order of integration used in inversion: 0 or 1
                                * unwrapping_column         -- TYPE:int.                      The reference column for unwrapping used in the processing
                                * I                         -- TYPE:array(ny,nx) UNITS:arb.   The complex-valued, onion-peeled interferogram
    
    '''
    
    #### Parse input parameters and load defaults
    emission_color = L1_dict['emission_color']
    params = global_params[emission_color]
    if sigma is None:
        sigma = params['sigma']
    if top_layer is None:
        top_layer = params['top_layer']
    if integration_order is None:
        integration_order = params['integration_order']
    if account_for_local_projection is None:
        account_for_local_projection = params['account_for_local_projection']
    if bin_size is None:
        bin_size = params['bin_size']
    bin_size = int(bin_size)

    ####  Load parameters from input dictionary
    Iraw = L1_dict['I_amp']*np.exp(1j*L1_dict['I_phase'])
    I_amp_uncertainty = L1_dict['I_amp_uncertainty']
    I_phase_uncertainty = L1_dict['I_phase_uncertainty']
    source_files = L1_dict['source_files']
    exp_time = L1_dict['exp_time']
    L1_fn = L1_dict['L1_fn']
    opd = L1_dict['optical_path_difference']
    sigma_opd = sigma * opd # Optical path difference, in units of wavelengths
    sensor = L1_dict['sensor']
    unwrapping_column = L1_dict['unwrapping_column']
    
    # Load parameters which are averaged from start to stop of exposure.
    icon_alt = (L1_dict['icon_alt_start'] + L1_dict['icon_alt_stop'])/2
    icon_lat = (L1_dict['icon_lat_start'] + L1_dict['icon_lat_stop'])/2
    icon_lon = circular_mean(L1_dict['icon_lon_start'], L1_dict['icon_lon_stop'])
    mighti_ecef_vectors = (L1_dict['mighti_ecef_vectors_start'] + L1_dict['mighti_ecef_vectors_stop'])/2
    tang_alt = (L1_dict['tang_alt_start'] + L1_dict['tang_alt_stop'])/2
    tang_lat = (L1_dict['tang_lat_start'] + L1_dict['tang_lat_stop'])/2
    tang_lon = circular_mean(L1_dict['tang_lon_start'], L1_dict['tang_lon_stop'])
    icon_ecef_ram_vector = (L1_dict['icon_ecef_ram_vector_start'] + L1_dict['icon_ecef_ram_vector_stop'])/2
    icon_velocity = (L1_dict['icon_velocity_start'] + L1_dict['icon_velocity_stop'])/2
    
    

    #### Remove Satellite Velocity
    icon_latlonalt = np.array([icon_lat, icon_lon, icon_alt])
    I = remove_satellite_velocity(Iraw, icon_latlonalt, icon_velocity, icon_ecef_ram_vector, mighti_ecef_vectors, sigma_opd)
                         
    #### Bin data
    I        = bin_image(bin_size, I)
    tang_lat = bin_array(bin_size, tang_lat)
    tang_lon = bin_array(bin_size, tang_lon, lon=True)
    tang_alt = bin_array(bin_size, tang_alt)
    ny, nx = np.shape(I)
    mighti_ecef_vectors_new = np.zeros((ny,nx,3))
    mighti_ecef_vectors_new[:,:,0] = bin_image(bin_size, mighti_ecef_vectors[:,:,0]) # bin each component separately
    mighti_ecef_vectors_new[:,:,1] = bin_image(bin_size, mighti_ecef_vectors[:,:,1]) # bin each component separately
    mighti_ecef_vectors_new[:,:,2] = bin_image(bin_size, mighti_ecef_vectors[:,:,2]) # bin each component separately
    mighti_ecef_vectors = mighti_ecef_vectors_new
    I_amp_uncertainty   = bin_uncertainty(bin_size, I_amp_uncertainty)
    I_phase_uncertainty = bin_uncertainty(bin_size, I_phase_uncertainty)
    
    
    
    #### Determine geographical locations of inverted wind
    lat, lon, alt = attribute_measurement_location(tang_lat, tang_lon, tang_alt,
                                                   integration_order=integration_order)
    
    
    #### Onion-peel interferogram
    Ip, phase, amp, phase_uncertainty, amp_uncertainty = perform_inversion(I, tang_alt, icon_alt, 
                           I_phase_uncertainty, I_amp_uncertainty, unwrapping_column,
                           top_layer=top_layer, integration_order=integration_order,
                           account_for_local_projection=account_for_local_projection)


    #### Transform from phase to wind
    f = phase_to_wind_factor(np.mean(sigma_opd)) # Use average OPD to analyze entire row
    v_inertial             = f * phase
    v_inertial_uncertainty = f * phase_uncertainty
        

    #### Calculate azimuth angles at measurement locations
    az = los_az_angle(icon_latlonalt, lat, lon, alt)

    #### Transform from inertial to rotating coordinate frame
    v = remove_Earth_rotation(v_inertial, az, lat, lon, alt)
    v_uncertainty = v_inertial_uncertainty.copy() # No appreciable uncertainty added in this process
    
    #### For reporting in output file, determine ecef vector at center of row
    mighti_ecef_vectors_center = mighti_ecef_vectors[:,nx/2,:]
    
    # Make a L2.1 dictionary
    L21_dict = {
             'los_wind'                     : v,
             'los_wind_error'               : v_uncertainty,
             'lat'                          : lat,
             'lon'                          : lon,
             'alt'                          : alt,
             'time_start'                   : L1_dict['time_start'],
             'time_stop'                    : L1_dict['time_stop'],
             'exp_time'                     : exp_time,
             'az'                           : az,
             'emission_color'               : emission_color,
             'sensor'                       : sensor,
             'resolution_along_track'       : np.nan, # TODO
             'resolution_cross_track'       : np.nan, # TODO
             'resolution_alt'               : np.nan, # TODO
             'icon_alt'                     : icon_alt,
             'icon_lat'                     : icon_lat,
             'icon_lon'                     : icon_lon,
             'fringe_amplitude'             : amp,
             'fringe_amplitude_error'       : amp_uncertainty,
             'mighti_ecef_vectors'          : mighti_ecef_vectors_center,
             'icon_velocity_ecef_vector'    : icon_velocity * icon_ecef_ram_vector,
             'file_creation_time'           : datetime.now(),
             'source_files'                 : np.concatenate((source_files,[L1_fn])),
             'bin_size'                     : bin_size,
             'top_layer'                    : top_layer,
             'integration_order'            : integration_order,
             'unwrapping_column'            : unwrapping_column,
             'I'                            : Ip,
    }
        
    return L21_dict
   
   



def _create_variable(ncfile, name, value, format_nc='f8', format_fortran='F', dimensions=(), zlib=True, complevel=6, 
                    shuffle=True,  depend_0=None, depend_1=None, depend_2=None, chunk_sizes=None, desc='', 
                    display_type='scalar', field_name='', fill_value=None,label_axis='', bin_location=0.5, 
                    time_base='FIXED: 1970 (POSIX)', time_scale='UTC', units='', valid_min=None, valid_max=None, 
                    notes='', var_type='data'):
    '''
    A helper function to write a variable to a netCDF file.
    
    INPUTS:
    
      *  Self evident from the parameters above. Notes:
      
            * fill_value = None --> default fill values will be used, if they exist. See netCDF4.default_fillvals
            * display_type: for now, 'scalar', 'altitude_profile', or 'image' will be used
            * var_type: one of 'data', 'support_data', 'metadata', 'ignore_data'
            * format_fortran: Used by ISTP. See http://www.cs.mtu.edu/~shene/COURSES/cs201/NOTES/chap05/format.html
            * except as specified above, if a variable attribute is left as the default None, it will not be written to the file
            
    OUTPUT:
    
      *  The netCDF4._netCDF4.Variable object that was created and to which was written
    
    '''    
        
    # Rudimentary error-checking:
    valid_var_types = ['data','support_data','metadata','ignore_data']
    if var_type not in valid_var_types:
        raise Exception('var_type="%s" is not valid. Try one of: %s' % (var_type, valid_var_types) )
    if len(desc) > 80:
        raise Exception('"desc" is too long (%i chars). Shorten to 80 characters:\n"%s"' % (len(desc),desc))
    if len(field_name) > 30:
        raise Exception('field_name="%s" is too long (%i chars). Shorten to 30 characters:\n"%s"' % (field_name,len(field_name)))
    if len(label_axis) > 10:
        raise Exception('label_axis="%s" is too long (%i chars). Shorten to 10 characters.' % (label_axis,len(label_axis)))
    
    # If fill value was not specified, use the default value, if it exists.
    # It will not exist for strings, for example, for which fill values
    # cannot be set. (TODO: is this right?)
    if fill_value is None and format_nc in netCDF4.default_fillvals.keys():
        fill_value = netCDF4.default_fillvals[format_nc]
    
    var = ncfile.createVariable(name, format_nc, dimensions=dimensions, zlib=zlib, complevel=complevel,
                               shuffle=shuffle, chunksizes=chunk_sizes, fill_value=fill_value)
    var.CatDesc            = desc
    var.Long_Name          = desc
    if chunk_sizes is not None: 
        var._ChunkingSizes = chunk_sizes
    var._DeflateLevel      = complevel
    var._Shuffle           = str(shuffle).lower()
    if depend_0 is not None:
        var.Depend_0       = depend_0
    if depend_1 is not None:
        var.Depend_1       = depend_1
    if depend_2 is not None:
        var.Depend_2       = depend_2
    var.Display_Type       = display_type 
    var.FieldNam           = field_name
    # Note: t_var._FillValue not expliclity needed since that is set by the createVariable function argument "fill_value"
    #var._FillValue         = fill_value
    if fill_value is not None:
        var.FillVal        = var._FillValue
    elif fill_value is None and format_nc == str: 
        # Special case for strings. Make sure to set FillVal even thought _FillValue can't be set
        var.FillVal        = ''
        
    var.Format             = format_fortran
    var.LablAxis           = label_axis
    var.Bin_Location       = bin_location
    var.Time_Base          = time_base
    var.Time_Scale         = time_scale
    var.Units              = units
    if valid_min is not None:
        var.ValidMin       = valid_min
        var.Valid_Min      = valid_min
    if valid_max is not None:
        var.ValidMax       = valid_max
        var.Valid_Max      = valid_max
    var.Var_Notes          = notes
    var.Var_Type           = var_type
    
    # If a fill_value was specified, and if there are any np.nan values in
    # the variable, replace them with the fill value.
    if fill_value is not None:
        # For sequences that are not strings:
        if hasattr(value,'__len__') and not isinstance(value,(str,unicode)):
            value[np.isnan(value)] = var._FillValue
        # For non-sequences and strings:
        elif np.isnan(value):
            value = var._FillValue
    
    # Assign value
    var[...] = value
    
    return var

   
   
    
    
def save_nc_level21(path, L21_dict, data_revision=0):
    '''
    Take the output of the Level 2.1 processing and save it as a NetCDF4 file in the official format.
    NetCDF4 file conventions taken from "Science Operations Center Data Product Conventions" Rev 0.5.
    
    INPUTS:
    
      *  path        -- TYPE:str.  The directory the file will be saved in, including trailing "/"
                                   (e.g., '/home/user/')
      *  L21_dict    -- TYPE:dict. A dictionary containing output variables of the Level 2.1 processing.
                                   See documentation for level1_dict_to_level21_dict(...) for details.
                                   
    OPTIONAL INPUTS:
    
      *  data_revision       -- TYPE:int,  The minor version of the data [0-999]. The major version is set
                                           by the software's major version.
                                   
    OUTPUTS:
    
      *  L21_fn      -- TYPE:str.  The full path to the saved file.
      
    TO-DO:
    
      * Maybe: Fill in more notes for each variable
      * Should dimensions be labeled the same as variables? Altitude/Vector/Epoch. Should Depend_0 point to vars or dims?
      * How can we append to global attributes History and MODS when the processing is re-run?
      
    '''
    
    data_version_major = software_version_major # enforced as per Data Product Conventions Document

    #################### Compile variables to write in file ######################
    ### Sensor:
    sensor = L21_dict['sensor']
    ### Timing:
    t_start = L21_dict['time_start']
    t_stop  = L21_dict['time_stop']
    t_mid   = t_start + timedelta(seconds=(t_stop - t_start).total_seconds()/2) # middle of exposure
    t_start_msec = (t_start - datetime(1970,1,1)).total_seconds()*1e3 # milliseconds since epoch
    t_stop_msec  = (t_stop  - datetime(1970,1,1)).total_seconds()*1e3
    t_mid_msec   = (t_mid   - datetime(1970,1,1)).total_seconds()*1e3
    t_start_msec = np.int64(np.round(t_start_msec)) # cast to signed 64 bit integer
    t_stop_msec  = np.int64(np.round(t_stop_msec)) 
    t_mid_msec   = np.int64(np.round(t_mid_msec))
    t_file  = datetime.now()   # time this file was created  
    ### Who's running this process
    user_name = getpass.getuser()
    ### Parent files
    parents = '' # This will go in global attr Parents
    for source_fn in L21_dict['source_files']:
        s = source_fn.split('/')[-1].split('.')
        pre = '.'.join(s[:-1])
        post = s[-1].upper()
        parents += '%s > %s, ' % (post, pre)
    if parents: parents = parents[:-2] # trim trailing comma
    


    L21_fn = 'ICON_L2_MIGHTI-%s_LINE-OF-SIGHT_WIND_%s_%s_v%02ir%03i.NC' % (sensor,L21_dict['emission_color'].upper(),
                                                           t_mid.strftime('%Y-%m-%d_%H%M%S'),
                                                           data_version_major, data_revision)
    L21_full_fn = '%s%s'%(path, L21_fn)
    ncfile = netCDF4.Dataset(L21_full_fn,mode='w',format='NETCDF4') 

    try:
        ########################## Global Attributes #################################
        ncfile.Acknowledgement =       ''.join(("This is a data product from the NASA Ionospheric Connection Explorer mission, ",
                                                "an Explorer launched in June 2017.\n",
                                                "\n",
                                                "Responsibility of the mission science falls to the Principal Investigator, ",
                                                "Dr. Thomas Immel at UC Berkeley.\n",
                                                "\n",
                                                "Validation of the L1 data products falls to the instrument lead ",
                                                "investigators/scientists.\n",
                                                "  * EUV  Dr. Eric Korpela\n",
                                                "  * FUV  Dr. Harald Frey\n",
                                                "  * MIGHTI  Dr. Chris Englert\n",
                                                "  * IVM  Dr. Roderick Heelis\n",
                                                "\n",
                                                "Validation of the L2 data products falls to those responsible for those products.\n",
                                                "  * O/N2  Dr. Andrew Stephan\n",
                                                "  * Daytime (EUV) O+ profiles  Dr. Andrew Stephan\n",
                                                "  * Nighttime (FUV) O+ profiles  Dr. Farzad Kamalabadi\n",
                                                "  * Neutral Wind profiles  Dr. Jonathan Makela\n",
                                                "  * Neutral Temperature profiles  Dr. Chris Englert\n",
                                                "\n",
                                                "Responsibility for Level 4 products are detailed on the ICON website ",
                                                "(http://icon.ssl.berkeley.edu).\n",
                                                "\n",
                                                "Overall validation of the products is overseen by the ICON Project Scientist ",
                                                "Dr. Scott England.\n",
                                                "\n",
                                                "NASA oversight for all products is provided by the Mission Scientist ",
                                                "Dr. Douglas Rowland.\n",
                                                "\n",
                                                "Users of these data should contact and acknowledge the Principal Investigator ",
                                                "Dr. Immel and the party directly responsible for the data product and the NASA ",
                                                "Contract Number NNG12FA45C from the Explorers Project Office." ))

        ncfile.ADID_Ref =                       'NASA Contract > NNG12FA45C'
        ncfile.Calibration_File =               ''
        ncfile.Conventions =                    'SPDF ISTP/IACG Modified for NetCDF'
        ncfile.Data_Level =                     'L2.1'
        ncfile.Data_Type =                      'DP21 > Data Product 2.1: Line-of-sight Wind Profile'
        ncfile.Data_Version_Major =             np.uint16(data_version_major)
        ncfile.Data_Revision =                  np.uint16(data_revision)
        ncfile.Data_Version =                   data_version_major + 0.001 * data_revision
        ncfile.Date_Stop =                      t_mid.strftime('%a, %d %b %Y, %Y-%m-%dT%H:%M:%S.%f')[:-3] + ' UTC' # single measurement: use midpoint
        ncfile.Date_Start =                     t_mid.strftime('%a, %d %b %Y, %Y-%m-%dT%H:%M:%S.%f')[:-3] + ' UTC' # single measurement: use midpoint
        ncfile.Description =                    'ICON MIGHTI Line-of-sight Winds (DP 2.1)'
        ncfile.Descriptor =                     'MIGHTI-%s > Michelson Interferometer for Global High-resolution ' % sensor+\
                                                'Thermospheric Imaging, Sensor %s' % sensor
        ncfile.Discipline =                     'Space Physics > Ionospheric Science'
        ncfile.File =                           L21_fn
        ncfile.File_Date =                      t_file.strftime('%a, %d %b %Y, %Y-%m-%dT%H:%M:%S.%f')[:-3] + ' UTC'
        ncfile.Generated_By =                   'ICON SDC > ICON UIUC MIGHTI L2.1 Processor v%s, B. J. Harding' % __version__
        ncfile.Generation_Date =                t_file.strftime('%Y%m%d')
        ncfile.History =                        'Version %i, %s, %s, ' % (software_version_major, user_name, t_file.strftime('%Y-%m-%dT%H:%M:%S')) +\
                                                'MIGHTI L2.1 Processor v%s ' % __version__
        ncfile.HTTP_LINK =                      'http://icon.ssl.berkeley.edu/Instruments/MIGHTI'
        ncfile.Instrument =                     'MIGHTI-%s' % sensor
        ncfile.Instrument_Type =                'Imagers (space)'
        ncfile.Link_Text =                      'MIGHTI Line-of-sight Wind Profile (DP 2.1)'
        ncfile.Link_Title =                     'ICON MIGHTI'
        ncfile.Logical_File_ID =                L21_fn[:-3]
        ncfile.Logical_Source =                 'ICON_L2_MIGHTI-%s_' % (sensor,)
        ncfile.Logical_Source_Description =     'MIGHTI Sensor %s - Line-of-sight Wind Profile'
        ncfile.Mission_Group =                  'Ionospheric Investigations'
        ncfile.MODS =                           ncfile.History
        ncfile.Parents =                        parents
        ncfile.PI_Affiliation =                 'UC Berkeley > SSL'
        ncfile.PI_Name =                        'T. J. Immel'
        ncfile.Project =                        'NASA > ICON'
        ncfile.Rules_of_Use =                   'Public Data for Scientific Use'
        ncfile.Software_Version =               'ICON SDC > ICON UIUC MIGHTI L2.1 Processor v%s' % __version__
        ncfile.Source_Name =                    'ICON > Ionospheric Connection Explorer'
        ncfile.Spacecraft_ID =                  'NASA > ICON - 493'
        ncfile.Text =                           'ICON explores the boundary between Earth and space - the ionosphere - ' +\
                                                'to understand the physical connection between our world and the immediate '+\
                                                'space environment around us. Visit \'http://icon.ssl.berkeley.edu\' for more details.'
        ncfile.Text_Supplement =                'See Harding et al. [2017], doi:10.1007/s11214-017-0359-3'
        ncfile.Time_Resolution =                '%.1f seconds' % L21_dict['exp_time']
        ncfile.Title =                          'ICON MIGHTI Line-of-sight Wind Profile (DP 2.1)'


        ################################## Dimensions ########################################
        n = len(L21_dict['alt'])
        ncfile.createDimension('Epoch',0)
        ncfile.createDimension('Altitude', n)
        ncfile.createDimension('Vector',3)
        ncfile.createDimension('Start_mid_stop',3)


        ################################## Variables #########################################

        prefix = 'ICON_L2_1_MIGHTI_%s_%s' % (sensor, L21_dict['emission_color'].upper()) # prefix of each variable,
                                                                                         # e.g., ICON_L2_1_MIGHTI_A_RED        
        ######### Timing Variables #########

        # Time midpoint (the official required "Epoch" variable)
        var = _create_variable(ncfile, 'EPOCH', t_mid_msec, 
                              dimensions=(),
                              format_nc='i8', format_fortran='I', desc='Sample time, midpoint of exposure. Number of msec since Jan 1, 1970.', 
                              display_type='scalar', field_name='Time', fill_value=None, label_axis='Time', bin_location=0.5,
                              units='ms', valid_min=0, valid_max=1000*365*86400e3, var_type='support_data', chunk_sizes=1,
                              notes='')

        # Time start/mid/stop
        var = _create_variable(ncfile, '%s_TIME'%prefix, np.array([t_start_msec, t_mid_msec, t_stop_msec]),
                              dimensions=('Start_mid_stop'),
                              format_nc='i8', format_fortran='I', desc='Sample time at start, mid, stop of exposure. Number of msec since Jan 1, 1970.', 
                              display_type='scalar', field_name='Time', fill_value=None, label_axis='Time', bin_location=[0.0,0.5,1.0],
                              units='ms', valid_min=0, valid_max=1000*365*86400e3, var_type='support_data', chunk_sizes=[1],
                              notes='')


        ######### Data Location and Direction Variables #########
        
        # Altitude
        val = L21_dict['alt']*1e3 # convert to meters
        var_alt = _create_variable(ncfile, '%s_ALTITUDE'%prefix, val, 
                              dimensions=('Altitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='WGS84 altitude of each wind sample', 
                              display_type='altitude_profile', field_name='Altitude', fill_value=None, label_axis='Altitude', bin_location=0.5,
                              units='m', valid_min=0, valid_max=1e10, var_type='support_data', chunk_sizes=[1],
                              notes='')

        # Latitude
        var = _create_variable(ncfile, '%s_LATITUDE'%prefix, L21_dict['lat'], 
                              dimensions=('Altitude'), depend_0 = var_alt.name,
                              format_nc='f8', format_fortran='F', desc='WGS84 latitude of each wind sample', 
                              display_type='altitude_profile', field_name='Latitude', fill_value=None, label_axis='Latitude', bin_location=0.5,
                              units='deg', valid_min=-90., valid_max=90., var_type='support_data', chunk_sizes=[1],
                              notes='')

        # Longitude
        var = _create_variable(ncfile, '%s_LONGITUDE'%prefix, L21_dict['lon'], 
                              dimensions=('Altitude'), depend_0 = var_alt.name,
                              format_nc='f8', format_fortran='F', desc='WGS84 longitude of each wind sample', 
                              display_type='altitude_profile', field_name='Longitude', fill_value=None, label_axis='Longitude', bin_location=0.5,
                              units='deg', valid_min=0., valid_max=360., var_type='support_data', chunk_sizes=[1],
                              notes='')

        # Azimuth angle of line of sight
        var = _create_variable(ncfile, '%s_LINE_OF_SIGHT_AZIMUTH'%prefix, L21_dict['az'], 
                              dimensions=('Altitude'), depend_0 = var_alt.name,
                              format_nc='f8', format_fortran='F', desc='Azimuth angle of the line of sight at the tangent point. Deg East of North.', 
                              display_type='altitude_profile', field_name='Line-of-sight Azimuth', fill_value=None, label_axis='Azimuth', bin_location=0.5,
                              units='deg', valid_min=0., valid_max=360., var_type='support_data', chunk_sizes=[1],
                              notes='')


        ######### Data Variables #########

        # Line-of-sight wind profile
        var = _create_variable(ncfile, '%s_LINE_OF_SIGHT_WIND'%prefix, L21_dict['los_wind'], 
                              dimensions=('Altitude'), depend_0 = var_alt.name,
                              format_nc='f8', format_fortran='F', desc='Line-of-sight horizontal wind profile. A positive wind is towards MIGHTI.', 
                              display_type='altitude_profile', field_name='Line-of-sight Wind', fill_value=None, label_axis='LoS Wind', bin_location=0.5,
                              units='m/s', valid_min=-1e10, valid_max=1e10, var_type='data', chunk_sizes=[1],
                              notes='')

        # Line-of-sight wind error profile
        var = _create_variable(ncfile, '%s_LINE_OF_SIGHT_WIND_ERROR'%prefix, L21_dict['los_wind_error'], 
                              dimensions=('Altitude'), depend_0 = var_alt.name,
                              format_nc='f8', format_fortran='F', desc='Line-of-sight Horizontal Wind Error Profile', 
                              display_type='altitude_profile', field_name='Line-of-sight Wind Error', fill_value=None, label_axis='Wind Error', bin_location=0.5,
                              units='m/s', valid_min=-1e10, valid_max=1e10, var_type='data', chunk_sizes=[1],
                              notes='')

        # Fringe amplitude profile (TODO: will this be replaced by a VER data product?)
        var = _create_variable(ncfile, '%s_FRINGE_AMPLTIUDE'%prefix, L21_dict['fringe_amplitude'], 
                              dimensions=('Altitude'), depend_0 = var_alt.name,
                              format_nc='f8', format_fortran='F', desc='Fringe Amplitude Profile', 
                              display_type='altitude_profile', field_name='Fringe Amplitude', fill_value=None, label_axis='Fringe Amp', bin_location=0.5,
                              units='arb', valid_min=-1e30, valid_max=1e30, var_type='data', chunk_sizes=[1],
                              notes='An approximate volume emission rate (VER) profile in arbitrary units. Technically this a profile of the visibility '+
                                    'of the fringes, which has a dependence on temperature and background emission.')

        # Fringe amplitude error profile (TODO: will this be replaced by a VER data product?)
        var = _create_variable(ncfile, '%s_FRINGE_AMPLTIUDE_ERROR'%prefix, L21_dict['fringe_amplitude_error'], 
                              dimensions=('Altitude'), depend_0 = var_alt.name,
                              format_nc='f8', format_fortran='F', desc='Fringe Amplitude Error Profile', 
                              display_type='altitude_profile', field_name='Fringe Amplitude Error', fill_value=None, label_axis='Amp Err', bin_location=0.5,
                              units='arb', valid_min=0, valid_max=1e30, var_type='data', chunk_sizes=[1],
                              notes='')


        ######### Other Metadata Variables #########

        # ICON velocity vector
        var = _create_variable(ncfile, '%s_SPACECRAFT_VELOCITY_VECTOR'%prefix, L21_dict['icon_velocity_ecef_vector'], 
                              dimensions=('Vector'),# depend_0 = 'Vector',
                              format_nc='f8', format_fortran='F', desc='ICON\'s velocity vector in Earth-Centered, Earth-fixed coordinates', 
                              display_type='scalar', field_name='ICON Velocity Vector', fill_value=None, label_axis='S/C Vel', bin_location=0.5,
                              units='m/s', valid_min=-1e10, valid_max=1e10, var_type='metadata', chunk_sizes=[1],
                              notes='')

        # ICON latitude
        var = _create_variable(ncfile, '%s_SPACECRAFT_LATITUDE'%prefix, L21_dict['icon_lat'], 
                              dimensions=(),
                              format_nc='f8', format_fortran='F', desc='The WGS84 latitude of ICON', 
                              display_type='scalar', field_name='Spacecraft Latitude', fill_value=None, label_axis='S/C Lat', bin_location=0.5,
                              units='deg', valid_min=-90., valid_max=90., var_type='metadata', chunk_sizes=1,
                              notes='')

        # ICON longitude
        var = _create_variable(ncfile, '%s_SPACECRAFT_LONGITUDE'%prefix, L21_dict['icon_lon'], 
                              dimensions=(),
                              format_nc='f8', format_fortran='F', desc='The WGS84 longitude of ICON', 
                              display_type='scalar', field_name='Spacecraft Longitude', fill_value=None, label_axis='S/C Lon', bin_location=0.5,
                              units='deg', valid_min=-0., valid_max=360., var_type='metadata', chunk_sizes=1,
                              notes='')

        # ICON altitude
        val = L21_dict['icon_alt']*1e3 # convert to m
        var = _create_variable(ncfile, '%s_SPACECRAFT_ALTITUDE'%prefix, val, 
                              dimensions=(),
                              format_nc='f8', format_fortran='F', desc='The WGS84 altitude of ICON', 
                              display_type='scalar', field_name='Spacecraft Altitude', fill_value=None, label_axis='S/C Alt', bin_location=0.5,
                              units='m', valid_min=0., valid_max=1e10, var_type='metadata', chunk_sizes=1,
                              notes='')

        # Along-track resolution
        val = L21_dict['resolution_along_track']*1e3 # convert to meters
        var = _create_variable(ncfile, '%s_RESOLUTION_ALONG_TRACK'%prefix, val, 
                              dimensions=(),
                              format_nc='f8', format_fortran='F', desc='The horizontal resolution in the spacecraft velocity direction', 
                              display_type='scalar', field_name='Along-Track Resolution', fill_value=None, label_axis='Hor Res AT', bin_location=0.5,
                              units='m', valid_min=0., valid_max=1e30, var_type='metadata', chunk_sizes=1,
                              notes='')

        # Cross-track resolution
        val = L21_dict['resolution_cross_track']*1e3 # convert to meters
        var = _create_variable(ncfile, '%s_RESOLUTION_CROSS_TRACK'%prefix, val, 
                              dimensions=(),
                              format_nc='f8', format_fortran='F', desc='The horizontal resolution perpendicular to the spacecraft velocity direction', 
                              display_type='scalar', field_name='Cross-Track Resolution', fill_value=None, label_axis='Hor Res CT', bin_location=0.5,
                              units='m', valid_min=0., valid_max=1e30, var_type='metadata', chunk_sizes=1,
                              notes='')

        # Altitude resolution
        var = _create_variable(ncfile, '%s_RESOLUTION_ALTITUDE'%prefix, L21_dict['resolution_alt']*1e3, # km to meters
                              dimensions=(),
                              format_nc='f8', format_fortran='F', desc='The vertical resolution', 
                              display_type='scalar', field_name='Vertical Resolution', fill_value=None, label_axis='Vert Res', bin_location=0.5,
                              units='m', valid_min=0., valid_max=1e30, var_type='metadata', chunk_sizes=1,
                              notes='')

        # MIGHTI ECEF vectors
        var = _create_variable(ncfile, '%s_LINE_OF_SIGHT_VECTOR'%prefix, L21_dict['mighti_ecef_vectors'], 
                              dimensions=('Altitude','Vector'), depend_0 = var_alt.name, # depend_1 = 'Vector',
                              format_nc='f8', format_fortran='F', desc='The look direction of each MIGHTI line of sight, as a vector in ECEF', 
                              display_type='altitude_profile', field_name='Line-of-sight Vector', fill_value=None, label_axis='LoS Vec', bin_location=0.5,
                              units='', valid_min=-1., valid_max=1., var_type='metadata', chunk_sizes=[1,3],
                              notes='')

        # Bin Size
        var = _create_variable(ncfile, '%s_BIN_SIZE'%prefix, L21_dict['bin_size'], 
                              dimensions=(),
                              format_nc='i1', format_fortran='I', desc='How many raw samples were binned vertically for each reported sample', 
                              display_type='scalar', field_name='Bin Size', fill_value=None, label_axis='Bin Size', bin_location=0.5,
                              units='', valid_min=0, valid_max=100000, var_type='metadata', chunk_sizes=1,
                              notes='')

        # Integration order
        var = _create_variable(ncfile, '%s_INTEGRATION_ORDER'%prefix, L21_dict['integration_order'], 
                              dimensions=(),
                              format_nc='i1', format_fortran='I', desc='Order used to discretize the integral for inversion: 0=Riemann, 1=Trapezoidal', 
                              display_type='scalar', field_name='Order', fill_value=None, label_axis='Order', bin_location=0.5,
                              units='', valid_min=0, valid_max=10, var_type='metadata', chunk_sizes=1,
                              notes='')

        # How the top layer was handled in the inversion
        var = _create_variable(ncfile, '%s_TOP_LAYER_MODEL'%prefix, L21_dict['top_layer'], 
                              dimensions=(),
                              format_nc=str, format_fortran='A', desc='How the top altitudinal layer is handled in the inversion: exp or thin', 
                              display_type='scalar', field_name='Top Layer', fill_value=None, label_axis='Top Layer', bin_location=0.5,
                              units='', valid_min=None, valid_max=None, var_type='metadata', chunk_sizes=1,
                              notes='')    
            
        ncfile.close()
        
    except: # make sure the file is closed
        ncfile.close()
        raise
    
    return L21_full_fn
    
    
    
    
    
    
def level1_to_level21_without_info_file(L1_fn, emission_color, L21_path , data_revision=0, sigma=None, top_layer=None, integration_order=None, 
                                        account_for_local_projection=None, bin_size = None):
    '''
    High-level function to apply the Level-1-to-Level-2.1 algorithm to a Level 1 file. This version
    of the function requires the user to input the arguments instead of specifying them with an 
    Information.TXT file, as will be done in the Science Data Center. A single Level 1 file can 
    become two Level 2.1 files (one per emission color).
    
    INPUTS:
    
      *  L1_fn               -- TYPE:str.  The full path to the Level 1 file to be processed
      *  emission_color      -- TYPE:str.  'red' or 'green'
      *  L21_path            -- TYPE:str.  The directory the Level 2.1 file will be saved in, including trailing "/"
                                           (e.g., '/home/user/')
      
    OPTIONAL INPUT:
    
      *  data_revision       -- TYPE:int,  The minor version of the data [0-999]. The major version is set
                                           by this software's major version. (default 0)

    MORE OPTIONAL INPUTS - If None, defaults from MIGHTI_L2.global_params will be used 
    
      *  sigma               -- TYPE:float, UNITS:m^-1. The wavenumber of the emission (1/wavelength)
      *  top_layer           -- TYPE:str, 'thin': assume VER goes to zero above top layer
                                          'exp':  assume VER falls off exponentially in altitude
      *  integration_order   -- TYPE:int, 0: Use Riemann-sum rule for discretizing line-of-sight integral
                                          1: Use trapezoidal rule for discretizing line-of-sight integral
      *  account_for_local_projection -- TYPE:bool. If False, a simple inversion is used.
                                         If True, the inversion accounts for the fact that the ray is not 
                                         perfectly tangent to each shell at each point along the ray.
      *  bin_size            -- TYPE:int, The number of rows of the interferogram to bin together to 
                                          improve statistics at the cost of altitude resolution.
                                           
    OUTPUTS:      
    
      *  L21_fn              -- TYPE:str.  The full path to the saved L2.1 file.

    '''
    
    # Parse inputs
    if emission_color not in ['red','green']:
        raise ValueError('Argument emission_color=\'%s\' not recognized. Use \'red\' or \'green\'.' % emission_color)
    # For other inputs, just pass them to the lower-level function. It will replace the Nones.
    
    # Read L1 file into a dictionary
    L1_dict = level1_to_dict(L1_fn, emission_color)
    
    # Perform L1 to L2.1 processing
    L21_dict = level1_dict_to_level21_dict(L1_dict, sigma, top_layer = top_layer, 
                                           integration_order = integration_order, 
                                           account_for_local_projection = account_for_local_projection, 
                                           bin_size = bin_size)
    
    
    # Save L2.1 file
    L21_fn = save_nc_level21(L21_path, L21_dict, data_revision)
    
    return L21_fn
    
    
    
    
    
def read_info_file(info_fn):
    '''
    Read the Information.TXT file that the Science Data Center provides, and return a dictionary
    of parameters and a list of files.
    
    INPUTS:
    
      * info_fn  -- TYPE:str.  Full path to an ASCII file in the following format:
      
                                        [PARAMETERS]
                                        Revision=001
                                        Directory=/path/to/wherever/
                                        <other parameters>

                                        [FILES]
                                        ICON_L1_MIGHTI-A_SCIENCE_2017-03-03_191803_v04r006.NC
                                        
                                        
    OUTPUTS:
    
      * info    -- TYPE:dict.         A dictionary of the parameters specified in the file. Keys
                                      and values are both strings.
      * files   -- TYPE:list of str.  A list of the files specified in the file.
      
    '''
    
    # Read the info file to extract parameters for the processing function
    info = {} # expected to have entries: 'Directory' and 'Revision'
    files = [] # files to process
    with open(info_fn, 'r') as f:
        # Read until the [Parameters] line
        line = f.readline()
        while line and '[parameters]' not in line.lower():
            line = f.readline()
        if not line:
            raise IOError('Information file format not understood: "[Parameters]" not found')
        # Read and save all the parameters, until a blank line
        line = f.readline().strip()
        while line:
            x = line.split('=')
            info[x[0]] = x[1]
            line = f.readline().strip()
        # Read until the [Parameters] line
        line = f.readline()
        while line and '[files]' not in line.lower():
            line = f.readline()
        if not line:
            raise IOError('Information file format not understood: "[Files]" not found')
        # Read until the end of the file, recording all the fns
        line = f.readline()
        while line:
            files.append(line.strip())
            line = f.readline()
            
    return info, files
    
    
    
    
    
def level1_to_level21(info_fn):
    '''
    Highest-level function to apply the Level-1-to-Level-2.1 algorithm to a Level 1 file, with 
    input arguments specified via an information file. Many files may be specified in the information
    file; this routine loops over all files specified. For each file, the processing is run twice:
    once for 'red' and once for 'green'. The output L2.1 files will be saved to the same directory 
    as the input L1 file.
    
    INPUTS:
    
      * info_fn  -- TYPE:str.  Full path to an ASCII file in the following format:
      
                                        [PARAMETERS]
                                        Revision=001
                                        Directory=/path/to/wherever/
                                        <other parameters>

                                        [FILES]
                                        ICON_L1_MIGHTI-A_SCIENCE_2017-03-03_191803_v04r006.NC
                                        
    OUTPUTS:   
    
      *  ret     -- TYPE:str. '0' if everything worked. If not, a human-readable error message for each file that failed
    
    '''    
    
    
    info, L1_fns = read_info_file(info_fn)
    
    # Parse the info
    # (0) Make sure there's a trailing "/" on the directory
    direc = info['Directory']
    if direc[-1] != '/':
        direc += '/'
    # (1) Add the directory to all the L1 files
    L1_full_fns = []
    for L1_fn in L1_fns:
        L1_full_fns.append(direc + L1_fn)
    # (2) Parse list of data revision numbers
    s = info['Revision'].split(',')
    data_revision = [int(x) for x in s]
    # if data_revision only has one entry, it should be applied to all input files
    if len(data_revision)==1:
        data_revision = [data_revision[0]]*len(L1_fns)
    assert len(L1_fns)==len(data_revision), "Length of revision list != Length of file list"
    
    
    # Loop and call the lower-level function which does all the real work.
    L21_fns = []
    failed_L1_fns = []
    failure_messages = []
    for L1_fn, rev in zip(L1_full_fns, data_revision):
        for emission_color in ['red','green']:
            try:
                L21_fn = level1_to_level21_without_info_file(L1_fn, emission_color, direc, data_revision = rev)
                L21_fns.append(L21_fn)
            except Exception as e:
                failed_L1_fns.append(L1_fn)
                failure_messages.append('Failed processing:\n\tL1_file = %s\n\tColor   = %s\n%s\n'%(L1_fn,emission_color,traceback.format_exc()))
                
    if not failure_messages: # Everything worked
        return '0' # Is this what Tori wants?
    
    else:
        s = '\n'.join(failure_messages)
        print(s)
        return s
                    
    
    
    
    
def level21_to_dict(L21_fns):
    ''' 
    Load a series of Level 2.1 files and return relevant variables in a dictionary. It is
    assumed that all files are from the same emission (red or green)
    
    INPUTS:
    
      *  L21_fns  -- TYPE:list of str.  The paths to the Level 2.1 files to be loaded.
      
    OUTPUTS:
    
      *  L21_dict -- TYPE:dict. A dictionary containing the following variables. Most 
                                are provided as arrays of shape (ny,nt), where ny is the number
                                of altitude samples and nt is the number of time samples.
                                
                  * lat             -- TYPE:array(ny,nt), UNITS:deg. Sample latitudes.
                  * lon             -- TYPE:array(ny,nt), UNITS:deg. Sample longitudes.
                  * alt             -- TYPE:array(ny,nt), UNITS:km.  Sample altitudes.
                  * los_wind        -- TYPE:array(ny,nt), UNITS:m/s. Line-of-sight wind component towards MIGHTI.
                  * los_wind_error  -- TYPE:array(ny,nt), UNITS:m/s. Error in los_wind variable.
                  * local_az        -- TYPE:array(ny,nt), UNITS:deg. Azimuth angle of vector pointing from 
                                       MIGHTI towards the sample location, at the sample location (deg E of N).
                  * amp             -- TYPE:array(ny,nt), UNITS:arb. Fringe amplitude at sample locations
                  * time            -- TYPE:array(nt).               Array of datetime objects, one per file.
                  * icon_lat        -- TYPE:array(nt),    UNITS:deg. Spacecraft latitude.
                  * icon_lon        -- TYPE:array(nt),    UNITS:deg. Spacecraft longitude.
                  * icon_alt        -- TYPE:array(nt),    UNITS:km.  Spacecraft altitude
                  * exp_time        -- TYPE:array(nt),    UNITS:sec. Exposure time of each sample.
                  * emission_color  -- TYPE:str,                     'red' or 'green'.
                  * sensor          -- TYPE:list(nt),                each element is 'A' or 'B'
                  * source_files    -- TYPE:list of str,             A copy of the input.
                  
    '''
    
    if len(L21_fns)==0:
        raise ValueError('No files specified')
    
    # Open the first file to see how many altitude bins there are
    fn = L21_fns[0]
    d = netCDF4.Dataset(fn)
    sens  = d.Instrument[-1] # 'A' or 'B'
    color = fn.split('/')[-1][:-31].split('_')[-1]     # RED or GREEN
    first_color = color
    prefix = 'ICON_L2_1_MIGHTI_%s_%s' % (sens, color)
    ny = len(d.variables['%s_ALTITUDE'%prefix])
    nt = len(L21_fns)
    d.close()
    
    lat = np.zeros((ny,nt))
    lon = np.zeros((ny,nt))
    alt = np.zeros((ny,nt))
    los_wind = np.zeros((ny,nt))
    los_wind_error = np.zeros((ny,nt))
    local_az = np.zeros((ny,nt))   
    amp = np.zeros((ny,nt))
    time = np.zeros(nt, dtype=object)
    exp_time = np.zeros(nt)
    icon_lat = np.zeros(nt)
    icon_lon = np.zeros(nt)
    icon_alt = np.zeros(nt)
    sensor = []

    for i in range(nt):
        fn = L21_fns[i]
        d = netCDF4.Dataset(fn)
        sens  = d.Instrument[-1] # 'A' or 'B'
        color = fn.split('/')[-1][:-31].split('_')[-1]     # RED or GREEN
        assert color == first_color, "Input files do not have the same emission color"
        prefix = 'ICON_L2_1_MIGHTI_%s_%s' % (sens, color)

        lat[:,i] =            d.variables['%s_LATITUDE' % prefix][...]
        lon[:,i] =            d.variables['%s_LONGITUDE' % prefix][...]
        alt[:,i] =     1e-3 * d.variables['%s_ALTITUDE' % prefix][...] # m to km
        los_wind[:,i] =       d.variables['%s_LINE_OF_SIGHT_WIND' % prefix][...]
        los_wind_error[:,i] = d.variables['%s_LINE_OF_SIGHT_WIND_ERROR' % prefix][...]
        local_az[:,i] =       d.variables['%s_LINE_OF_SIGHT_AZIMUTH' % prefix][...]
        amp[:,i] =            d.variables['%s_FRINGE_AMPLTIUDE' % prefix][...]
        icon_lat[i] =         d.variables['%s_SPACECRAFT_LATITUDE' % prefix][...].item()
        icon_lon[i] =         d.variables['%s_SPACECRAFT_LONGITUDE' % prefix][...].item()
        icon_alt[i] =         d.variables['%s_SPACECRAFT_ALTITUDE' % prefix][...].item()
        time_msec =           d.variables['EPOCH'][...].item()
        time[i] = datetime(1970,1,1) + timedelta(seconds = 1e-3*time_msec)
        exp_time[i] = float(d.Time_Resolution.split(' ')[0])
        sensor.append(sens)
        d.close()    
    
    L21_dict = {}
    L21_dict['lat'] = lat
    L21_dict['lon'] = lon
    L21_dict['alt'] = alt
    L21_dict['los_wind'] = los_wind
    L21_dict['los_wind_error'] = los_wind_error
    L21_dict['local_az'] = local_az
    L21_dict['amp'] = amp
    L21_dict['time'] = time
    L21_dict['icon_lat'] = icon_lat
    L21_dict['icon_lon'] = icon_lon
    L21_dict['icon_alt'] = icon_alt
    L21_dict['exp_time'] = exp_time
    L21_dict['emission_color'] = color.lower()
    L21_dict['sensor'] = sensor
    L21_dict['source_files'] = L21_fns
    
    return L21_dict

    
    
    
    
############################################################################################################
##########################################       Level 2.2       ###########################################
############################################################################################################
   

    
def level21_dict_to_level22_dict(L21_A_dict, L21_B_dict, sph_asym_thresh = None, time_start = None, time_stop = None):
    '''
    Given Level 2.1 data from MIGHTI A and MIGHTI B, process it with the Level 2.2 algorithm. 
    This entails interpolating line-of-sight wind measurements from MIGHTI A and B to a 
    common grid, and rotating to a geographic coordinate system to derive vector horizontal winds. 
    The input files should span 24 hours (0 UT to 23:59:59 UT), with ~20 minutes of extra files
    on either side.
    
    INPUTS:
    
      *  L21_A_dict      -- TYPE:dict.  The dictionary corresponding to 24+ hours of MIGHTI A measurements 
                                        for a single emission color. See "level21_to_dict" for required keys.
      *  L21_B_dict      -- TYPE:dict.  The dictionary corresponding to 24+ hours of MIGHTI B measurements
                                        for a single emission color, which is the same as for the A measurements.
                                        See "level21_to_dict" for required keys.
                                        
    OPTIONAL INPUTS:
    
      *  sph_asym_thresh -- TYPE:float.    Relative difference in emission rate measured by A and B, beyond which
                                           the spherical-asymmetry flag will be raised. Technically, it should be
                                           "fringe amplitude" instead of "emission rate" due to the temperature
                                           dependence. Relative difference is defined as abs(A-B)/((A+B)/2). If 
                                           None (default), the default from MIGHTI_L2.global_params will be used.
      *  time_start      -- TYPE:datetime. A timezone-naive datetime in UT, specifying the beginning of the interval
                                           which the Level 2.2 data product should span. (Some L2.1 files from before
                                           the start time are needed to handle the crossover). If None (default), 
                                           the start time defaults to the first 0 UT after the first input file's time.
                                           Note that the time changes from the top to the bottom of a L2.2 profile. We
                                           use the top of the field of view as the reference. This means that it's
                                           possible for some time samples to occur before the start time or after the
                                           stop time.
      *  time_stop       -- TYPE:datetime. A timezone-naive datetime in UT, specifying the end of the interval
                                           which the Level 2.2 data product should span. (Some L2.1 files from after
                                           the start time are needed to handle the crossover). If None (default), 
                                           the stop time defaults to 24 hours after the start time.
                                        
    OUTPUTS:
    
      *  L22_dict        -- TYPE:dict.  A dictionary containing the following variables. Most are given as 
                                        arrays with shape (ny,nx) where ny is the number of altitude grid
                                        points, and nx is the number of horizontal grid points.
                                        
                    * lat             -- TYPE:array(ny,nx),    UNITS:deg.  Latitude of each point on the grid.
                    * lon             -- TYPE:array(ny,nx),    UNITS:deg.  Longitude of each point on the grid [0-360]
                    * lon_unwrapped   -- TYPE:array(ny,nx),    UNITS:deg.  Same as lon, but with 0/360 jumps removed
                    * alt             -- TYPE:array(ny,nx),    UNITS:km.   Altitude of each point on the grid.
                    * u               -- TYPE:array(ny,nx),    UNITS:m/s.  Estimated zonal wind (positive eastward).
                    * v               -- TYPE:array(ny,nx),    UNITS:m/s.  Estimated meridional wind (positive northward).
                    * u_error         -- TYPE:array(ny,nx),    UNITS:m/s.  Uncertainty in u.
                    * v_error         -- TYPE:array(ny,nx),    UNITS:m/s.  Uncertainty in v.
                    * error_flags     -- TYPE:array(ny,nx,ne), UNITS:none. The error flags (either 0 or 1) for each point
                                                                          in the grid. Each point has a number of error flags, 
                                                                          which are set to 1 under the following 
                                                                          circumstances:
                                                                          
                                                                            * 0  = missing A file
                                                                            * 1  = missing B file
                                                                            * 2  = A signal too weak
                                                                            * 3  = B signal too weak
                                                                            * 4  = A did not sample this altitude
                                                                            * 5  = B did not sample this altitude
                                                                            * 6  = A sample exists but equals np.nan
                                                                            * 7  = B sample exists but equals np.nan
                                                                            * 8  = spherical asymmetry: A&B VER 
                                                                                   estimates disagree
                                                                            * 9  = unknown error
                                                                            
                   * time            -- TYPE:array(ny,nx),    UNITS:none. The average between the time of the MIGHTI A 
                                                                          and B measurements that contribute to this 
                                                                          grid point, given as a datetime object. 
                   * time_start      -- TYPE:datetime                     The start time for defining the reconstruction grid
                   * time_stop       -- TYPE:datetime                     The stop time for defining the reconstruction grid
                   * time_delta      -- TYPE:array(ny,nx),    UNITS:s.    The difference between the time of the MIGHTI
                                                                          A and B measurements that contribute to this 
                                                                          grid point.
                   * ver_A           -- TYPE:array(ny,nc),    UNITS:none. The fringe amplitude measured by MIGHTI A (roughly
                                                                          proportional to VER)
                   * ver_B           -- TYPE:array(ny,nc),    UNITS:none. The fringe amplitude measured by MIGHTI B (roughly
                                                                          proportional to VER)
                   * ver             -- TYPE:array(ny,nc),    UNITS:none. Mean of the above two quantitiies.
                   * ver_rel_diff    -- TYPE:array(ny,nx),    UNITS:none. The difference between the fringe amplitude 
                                                                          of the MIGHTI A and B measurements that 
                                                                          contribute to this grid point, divided by the
                                                                          mean. When this is high, it indicates that 
                                                                          spherical asymmetry may be a problem.
                   * emission_color  -- TYPE:str,                         'red' or 'green'.        
                   * source_files    -- TYPE:list of str,                 All the files used to create the data product,
                                                                          including the full paths.
                    
    TODO:
    
        * only save the parent files that were actually used.
        
    '''


    
    
    ################################## Parse Inputs ####################################
    emission_color = L21_A_dict['emission_color']
    params = global_params[emission_color]
    if sph_asym_thresh is None:
        sph_asym_thresh = params['sph_asym_thresh']
    
    if time_start is None:
        # Default to first 0 UT after first file
        t_first = min(L21_A_dict['time'][0], L21_B_dict['time'][0]) # first file time
        time_start = datetime(t_first.year, t_first.month, t_first.day) + timedelta(days=1)
        
    if time_stop is None:
        # Default to 24 hours after the start time
        time_stop = time_start + timedelta(hours=24)
    
    assert L21_A_dict['emission_color'] == L21_B_dict['emission_color'], "Files for A and B are for different emissions"
    
    
    lat_A      =       L21_A_dict['lat']
    lon_A_raw  =       L21_A_dict['lon']
    alt_A      =       L21_A_dict['alt']
    los_wind_A =       L21_A_dict['los_wind']
    los_wind_A_err =   L21_A_dict['los_wind_error']
    local_az_A =       L21_A_dict['local_az']
    amp_A      =       L21_A_dict['amp']
    time_A     =       L21_A_dict['time']
    exp_time_A =       L21_A_dict['exp_time']
    emission_color =   L21_A_dict['emission_color']
    N_alts_A, N_times_A = np.shape(lat_A)

    lat_B      =       L21_B_dict['lat']
    lon_B_raw  =       L21_B_dict['lon']
    alt_B      =       L21_B_dict['alt']
    los_wind_B =       L21_B_dict['los_wind']
    los_wind_B_err =   L21_B_dict['los_wind_error']
    local_az_B =       L21_B_dict['local_az']
    amp_B      =       L21_B_dict['amp']
    time_B     =       L21_B_dict['time']
    exp_time_B =       L21_B_dict['exp_time']
    emission_color =   L21_B_dict['emission_color']
    N_alts_B, N_times_B = np.shape(lat_B)
    
    ####################### Define reconstruction grid: lon/alt ########################

    # Unwrap the sample longitude arrays to avoid 0/360 jumps
    lon_A = lon_A_raw.copy()
    lon_B = lon_B_raw.copy()
    lon_A[:,0] = fix_longitudes(lon_A[:,0], lon_A[-1,0]) # Use top first A longitude as target
    lon_B[:,0] = fix_longitudes(lon_B[:,0], lon_A[-1,0]) # Use top first A longitude as target
    for i in range(N_alts_A):
        lon_A[i,:] = fix_longitudes(lon_A[i,:], lon_A[i,0])
    for i in range(N_alts_B):
        lon_B[i,:] = fix_longitudes(lon_B[i,:], lon_B[i,0])

    # Determine start longitude
    # This should be the longitude of the tangent point at the top of the profile for the first
    # exposure after the start time, averaged between A and B.
    iA = bisect.bisect(time_A, time_start) # First A file after start time.
    iB = bisect.bisect(time_B, time_start) # First B file after start time.
    start_lon_A = lon_A[-1,iA]
    start_lon_B = lon_B[-1,iB]
    assert abs(start_lon_A - start_lon_B) < 90.,"A and B start longitudes are off by 360 deg. Bug in unwrapping code."
    start_lon = np.mean([start_lon_A, start_lon_B])

    # Determine stop longitude
    # This should be the longitude of the tangent point at the top of the profile for the first
    # exposure before the stop time, averaged between A and B.
    iA = bisect.bisect(time_A, time_stop) - 1 # First A file before (or equal to) stop time
    iB = bisect.bisect(time_B, time_stop) - 1 # First B file before (or equal to) stop time
    stop_lon_A = lon_A[-1,iA]
    stop_lon_B = lon_B[-1,iB]
    assert abs(stop_lon_A - stop_lon_B) < 90.,"A and B start longitudes are off by 360 deg. Bug in unwrapping code."
    stop_lon = np.mean([stop_lon_A, stop_lon_B])
    
    time_min = min(min(time_A), min(time_B))
    time_max = max(max(time_A), max(time_B))
    assert stop_lon > start_lon, 'No files found between time_start="%s" and time_stop="%s". Files provided span from "%s" to "%s"'%(time_start, \
                                                                                   time_stop, time_min, time_max)

    # Determine how finely to define longitude grid based on minimum L2.1 sampling rate
    lon_res_A = (np.diff(lon_A,axis=1)).min()
    lon_res_B = (np.diff(lon_B,axis=1)).min()
    lon_res = min(lon_res_A, lon_res_B)
    # Define longitude grid
    lon_vec = np.arange(start_lon, stop_lon, lon_res)

    # Define altitude grid based on the min and max in the L2.1 data
    # Define altitude resolution based upon the resoluation of L2.1 data
    alt_min = min(alt_A.min(), alt_B.min())
    alt_max = max(alt_A.max(), alt_B.max())
    nalts = max(np.shape(alt_A)[0], np.shape(alt_B)[0])
    alt_vec = np.linspace(alt_min, alt_max, nalts)
    alt_res = alt_vec[1] - alt_vec[0]

    # Define 2D reconstruction grid based on lon and alt
    lon,alt = np.meshgrid(lon_vec, alt_vec)
    N_alts, N_lons = np.shape(lon)

    ############### Interpolate values to reconstruction grid ##################
    # Use bilinear interpolation. This is somewhat complicated because
    # the sample grid is not exactly regular, but it's close, and 
    # we are approximating it as such. We're implementing our own
    # bilinear interpolation so we can control extrapolation in 
    # longitude and altitude as desired. Bilinear interpolation is 
    # used because it is insensitive to the units used to define
    # the sample grid.
    # This proceeds in 4 steps:
    # 1) Setup
    # 2) Error flagging
    # 3) Interpolation
    # 4) Inverting (i.e., rotating LoS winds to cardinal)
            
    # Output variables, which will be defined on the reconstruction grid
    U = np.nan*np.zeros(np.shape(lon))                # zonal wind
    V = np.nan*np.zeros(np.shape(lon))                # meridional wind
    U_err = np.nan*np.zeros(np.shape(lon))            # zonal wind uncertainty
    V_err = np.nan*np.zeros(np.shape(lon))            # meridional wind uncertainty
    lat = np.nan*np.zeros(np.shape(lon))              # latitude
    time = np.empty(np.shape(lon), dtype=object)      # time ascribed to L2.2 data point (as datetime objects)
    time_delta = np.nan*np.zeros(np.shape(lon))       # difference between A and B times used (seconds)
    ver_A = np.nan*np.zeros(np.shape(lon))            # fringe amplitude from A (related to VER)
    ver_B = np.nan*np.zeros(np.shape(lon))            # fringe amplitude from B (related to VER)
    ver   = np.nan*np.zeros(np.shape(lon))            # fringe amplitude (mean of A and B)
    ver_rel_diff = np.nan*np.zeros(np.shape(lon))     # relative difference in A and B VER
    error_flags = np.zeros((N_alts, N_lons, 10))      # Error flags, one set per grid point. See above for definition.
    
    # Loop over the reconstruction altitudes
    for i in range(N_alts):
            
        alt_pt = alt_vec[i]
        # Create a list of longitudes, one per A and B file, which have been
        # interpolated to this altitude.
        lon_list_A = np.zeros(N_times_A)
        lon_list_B = np.zeros(N_times_B)
        for k in range(N_times_A):
            lon_list_A[k] = interpolate_linear(alt_A[:,k], lon_A[:,k], alt_pt)
        for k in range(N_times_B):
            lon_list_B[k] = interpolate_linear(alt_B[:,k], lon_B[:,k], alt_pt)
        
        # Loop over the reconstruction longitudes
        for k in range(N_lons):
            
            lon_pt = lon_vec[k]
            
            # Find the file to the left and right in longitude. 
            kA0 = bisect.bisect(lon_list_A, lon_pt) - 1
            kA1 = kA0 + 1
            kB0 = bisect.bisect(lon_list_B, lon_pt) - 1
            kB1 = kB0 + 1
            
            
            
            ##################### Error Flagging ##########################
            # Never extrapolate in longitude. This error should not normally happen, and 
            # probably indicates an entire missing orbit or an extended calibration routine.
            # Mark as missing, and continue.
            if kA0 < 0 or kA1 >= N_times_A or kB0 < 0 or kB1 >= N_times_B:
                if kA0 < 0 or kA1 >= N_times_A:
                    error_flags[i,k,0] = 1
                if kB0 < 0 or kB1 >= N_times_B:
                    error_flags[i,k,1] = 1
                continue
            
            # Determine if there are "missing" files by checking the time between the straddling
            # files we just found and comparing to the exposure time of the files.
            # If so, throw error flag and continue.
            # Note that it's the exposure time of the first file that matters here.
            missing_A = (time_A[kA1] - time_A[kA0]).total_seconds() > 1.5*exp_time_A[kA0]
            missing_B = (time_B[kB1] - time_B[kB0]).total_seconds() > 1.5*exp_time_B[kB0]
            if missing_A or missing_B:
                if missing_A:
                    error_flags[i,k,0] = 1
                if missing_B:
                    error_flags[i,k,1] = 1
                continue
                    
            # If the desired altitude is outside the range of altitudes sampled by the 
            # instruments, throw error flag and continue.
            # For this, allow some wiggle room to handle case where MIGHTI A samples
            # at 90.01 km but we wanted 90.00 km.
            altmin_A = max(min(alt_A[:,kA0]), min(alt_A[:,kA1])) - alt_res
            altmin_B = max(min(alt_B[:,kB0]), min(alt_B[:,kB1])) - alt_res
            altmax_A = min(max(alt_A[:,kA0]), max(alt_A[:,kA1])) + alt_res
            altmax_B = min(max(alt_B[:,kB0]), max(alt_B[:,kB1])) + alt_res
            if alt_pt > min(altmax_A, altmax_B) or alt_pt < max(altmin_A, altmin_B):
                if alt_pt > altmax_A or alt_pt < altmin_A:
                    error_flags[i,k,4] = 1
                if alt_pt > altmax_B or alt_pt < altmin_B:
                    error_flags[i,k,5] = 1
                continue
            
            
            
            ######################## Interpolating ############################
            # If it passed all the error checks, perform bilinear interpolation (altitude, then longitude).
            # Variables to interpolate to this point:
            #   - los_wind (A and B)
            #   - az       (A and B)
            #   - lat      (A and B, to be averaged)
            #   - time     (A and B, to be averaged and subtracted)
            #   - ver      (A and B, to be compared)
            
            def bilinear_interp(lon_AB, alt_AB, val, prop_err = False, valerr = None):
                '''
                Helper function that will bilinearly interpolate the Nx2 array "val", sampled
                at the Nx2 array of points described by lon_AB and alt_AB, to the point 
                currently under consideration (i.e., lon_pt, alt_pt).
                
                Optional input to propagate error from original to interpolated value. If
                prop_err = True, valerr must be specified (as the error of each value of
                val), and the interpolated error will be provided an additional output.
                '''
                
                if prop_err and valerr is None:
                    raise Exception('If prop_err = True, then valerr must be specified')
                
                if not prop_err:
                    # Do interpolate of value to the desired altitude, for each longitude
                    val_0 = interpolate_linear(alt_AB[:,0], val[:,0], alt_pt)
                    val_1 = interpolate_linear(alt_AB[:,1], val[:,1], alt_pt)
                    # Interpolate the longitude coordinate to the desired altitude
                    lon_0 = interpolate_linear(alt_AB[:,0], lon_AB[:,0], alt_pt)
                    lon_1 = interpolate_linear(alt_AB[:,1], lon_AB[:,1], alt_pt)
                    # Do interpolation to the desired longitude
                    val_pt = interpolate_linear([lon_0, lon_1], [val_0, val_1], lon_pt,
                                                          extrapolation='none')
                    return val_pt
                
                else: # prop_err is True
                    # Do interpolation of value to the desired altitude, for each longitude
                    val_0, val_0_err = interpolate_linear(alt_AB[:,0], val[:,0], alt_pt, 
                                                          prop_err = True, yerr = valerr[:,0])
                    val_1, val_1_err = interpolate_linear(alt_AB[:,1], val[:,1], alt_pt, prop_err = True, yerr = valerr[:,1])
                    # Interpolate the longitude coordinate to the desired altitude
                    lon_0 = interpolate_linear(alt_AB[:,0], lon_AB[:,0], alt_pt)
                    lon_1 = interpolate_linear(alt_AB[:,1], lon_AB[:,1], alt_pt)
                    # Do interpolation to the desired longitude
                    val_pt, val_pt_err = interpolate_linear([lon_0, lon_1], [val_0, val_1], lon_pt,
                                                            extrapolation='none', 
                                                            prop_err = True, yerr = [val_0_err, val_1_err])
                    return val_pt, val_pt_err
                    
            
            los_wind_A_pt, los_wind_A_pt_err = \
                            bilinear_interp(lon_A[:,kA0:kA1+1], alt_A[:,kA0:kA1+1], los_wind_A[:,kA0:kA1+1],
                                            prop_err = True, valerr = los_wind_A_err[:,kA0:kA1+1])
            los_wind_B_pt, los_wind_B_pt_err = \
                            bilinear_interp(lon_B[:,kB0:kB1+1], alt_B[:,kB0:kB1+1], los_wind_B[:,kB0:kB1+1],
                                            prop_err = True, valerr = los_wind_B_err[:,kB0:kB1+1])
            local_az_A_pt = bilinear_interp(lon_A[:,kA0:kA1+1], alt_A[:,kA0:kA1+1], local_az_A[:,kA0:kA1+1])
            local_az_B_pt = bilinear_interp(lon_B[:,kB0:kB1+1], alt_B[:,kB0:kB1+1], local_az_B[:,kB0:kB1+1])
            lat_A_pt      = bilinear_interp(lon_A[:,kA0:kA1+1], alt_A[:,kA0:kA1+1], lat_A     [:,kA0:kA1+1])
            lat_B_pt      = bilinear_interp(lon_B[:,kB0:kB1+1], alt_B[:,kB0:kB1+1], lat_B     [:,kB0:kB1+1])
            ver_A_pt      = bilinear_interp(lon_A[:,kA0:kA1+1], alt_A[:,kA0:kA1+1], amp_A     [:,kA0:kA1+1])
            ver_B_pt      = bilinear_interp(lon_B[:,kB0:kB1+1], alt_B[:,kB0:kB1+1], amp_B     [:,kB0:kB1+1])
            
            # Interpolate time, which is more complicated because it's a datetime object
            t_A_0 = time_A[kA0]
            t_A_1 = time_A[kA1]
            t_B_0 = time_B[kB0]
            t_B_1 = time_B[kB1]
            # Interpolate the longitude coordinate to the desired altitude
            lon_A_0 = interpolate_linear(alt_A[:,kA0], lon_A[:,kA0], alt_pt)
            lon_A_1 = interpolate_linear(alt_A[:,kA1], lon_A[:,kA1], alt_pt)
            lon_B_0 = interpolate_linear(alt_B[:,kB0], lon_B[:,kB0], alt_pt)
            lon_B_1 = interpolate_linear(alt_B[:,kB1], lon_B[:,kB1], alt_pt)
            # Interpolate time gap, and add to time
            tgap_A = (t_A_1 - t_A_0).total_seconds()
            tgap_B = (t_B_1 - t_B_0).total_seconds()
            toff_A = interpolate_linear([lon_A_0, lon_A_1], [0, tgap_A], lon_pt, extrapolation='none')
            toff_B = interpolate_linear([lon_B_0, lon_B_1], [0, tgap_B], lon_pt, extrapolation='none')
            t_A = t_A_0 + timedelta(seconds=(toff_A))
            t_B = t_B_0 + timedelta(seconds=(toff_B))
            
            
            ############################ Inversion #############################
            # Coordinate transformation of winds from lines of sight to cardinal directions
            # Construct LoS winds in vector y
            y = np.array([-los_wind_A_pt, -los_wind_B_pt])
            # Coordinate transform (the heart of the L2.2 processing)
            azA_rad = np.deg2rad(local_az_A_pt)
            azB_rad = np.deg2rad(local_az_B_pt)
            A = np.array([[np.sin(azA_rad), np.cos(azA_rad)],
                          [np.sin(azB_rad), np.cos(azB_rad)]])
            invA = np.linalg.inv(A) # explicitly compute inverse
            x = invA.dot(y)
            u = x[0]
            v = x[1]
            # propagate uncertainties
            Sig_y = np.array([[los_wind_A_pt_err**2, 0.0],
                              [0.0, los_wind_B_pt_err**2]]) # covariance matrix of y
            Sig_x = invA.dot(Sig_y.dot(invA.T)) # standard linear error propagation
            u_err = np.sqrt(Sig_x[0,0])
            v_err = np.sqrt(Sig_x[1,1])

            
            ###################### Final error flagging #######################
            # Check spherical symmetry
            ver_rel_diff_pt = abs(ver_A_pt - ver_B_pt)/np.mean([ver_A_pt,ver_B_pt])
            if ver_rel_diff_pt > sph_asym_thresh:
                error_flags[i,k,8] = 1
        
            # Check if L2.1 data were nan
            if np.isnan(los_wind_A_pt):
                error_flags[i,k,6] = 1
            if np.isnan(los_wind_B_pt):
                error_flags[i,k,7] = 1
            if np.isnan(u) or np.isnan(v) and all(error_flags[i,k,:] == 0): # Unknown error
                error_flags[i,k,9] = 1
                
                
            # Fill in all the relevant variables at this grid point
            U[i,k] = u
            V[i,k] = v
            U_err[i,k] = u_err
            V_err[i,k] = v_err
            lat[i,k] = (lat_A_pt + lat_B_pt)/2
            time[i,k] = t_A + timedelta(seconds=(t_B-t_A).total_seconds()/2)
            time_delta[i,k] = (t_B-t_A).total_seconds()
            ver_A[i,k] = ver_A_pt
            ver_B[i,k] = ver_B_pt
            ver[i,k]   = (ver_A_pt + ver_B_pt)/2.
            ver_rel_diff[i,k] = ver_rel_diff_pt
                
    
    # Create dictionary to be returned
    L22_dict = {}
    L22_dict['lat'] = lat
    L22_dict['lon'] = np.mod(lon, 360.)
    L22_dict['lon_unwrapped'] = lon
    L22_dict['alt'] = alt
    L22_dict['u'] = U
    L22_dict['v'] = V
    L22_dict['u_error'] = U_err
    L22_dict['v_error'] = V_err
    L22_dict['error_flags'] = error_flags
    L22_dict['time'] = time
    L22_dict['time_delta'] = time_delta
    L22_dict['time_start'] = time_start
    L22_dict['time_stop'] = time_stop
    L22_dict['ver_A'] = ver_A
    L22_dict['ver_B'] = ver_B
    L22_dict['ver'] = ver
    L22_dict['ver_rel_diff'] = ver_rel_diff
    L22_dict['emission_color'] = emission_color
    L22_dict['source_files'] = np.concatenate((L21_A_dict['source_files'], L21_B_dict['source_files']))
    
    return L22_dict





def save_nc_level22(path, L22_dict, data_revision = 0):
    '''
    Take the output of the Level 2.2 processing and save it as a NetCDF4 file in the official format.
    NetCDF4 file conventions taken from "Science Operations Center Data Product Conventions" Rev 0.5.
    
    INPUTS:
    
      *  path        -- TYPE:str.  The directory the file will be saved in, including trailing "/"
                                   (e.g., '/home/user/')
      *  L22_dict    -- TYPE:dict. A dictionary containing output variables of the Level 2.2 processing.
                                   See documentation for level21_dict_to_level22_dict(...) for details.
                     
    OPTIONAL INPUTS:
    
      *  data_revision       -- TYPE:int,  The minor version of the data [0-999]. The major version is set
                                           by the software's major version.
                                           
    OUTPUTS:
    
      *  L22_fn      -- TYPE:str.  The full path to the saved file.
      
    TO-DO:
    
      * Maybe: Fill in more notes for each variable
      * I'm confused about Dimensions and Depend_0, Depend_1, etc:
              * Should dimensions be labeled the same as variables? Altitude/Vector/Epoch. 
              * Should Depend_0 point to Variables or Dimensions?
        
    '''
   
    data_version_major = software_version_major # enforced as per Data Product Conventions Document
    
    #################### Compile variables to write in file ######################
    ### Timing:
    t_all = filter(None, L22_dict['time'].flatten()) # Extract all non-None grid times as a 1-D array
    t_start = L22_dict['time_start']
    t_stop  = L22_dict['time_stop']
    t_mid   = t_start + timedelta(seconds=(t_stop - t_start).total_seconds()/2) # midpoint time
    t_start_msec = (t_start - datetime(1970,1,1)).total_seconds()*1e3 # milliseconds since epoch
    t_stop_msec  = (t_stop  - datetime(1970,1,1)).total_seconds()*1e3
    t_mid_msec   = (t_mid   - datetime(1970,1,1)).total_seconds()*1e3
    t_start_msec = np.int64(np.round(t_start_msec)) # cast to signed 64 bit integer
    t_stop_msec  = np.int64(np.round(t_stop_msec)) 
    t_mid_msec   = np.int64(np.round(t_mid_msec))
    t_file  = datetime.now()   # time this file was created  
    ### Who's running this process
    user_name = getpass.getuser()
    ### Parent files
    parents = '' # This will go in global attr Parents
    for source_fn in L22_dict['source_files']:
        s = source_fn.split('/')[-1].split('.')
        pre = '.'.join(s[:-1])
        post = s[-1].upper()
        parents += '%s > %s, ' % (post, pre)
    if parents: parents = parents[:-2] # trim trailing comma


    ######################### Open file for writing ##############################
    L22_fn = 'ICON_L2_MIGHTI_VECTOR_WIND_%s_%s_v%02ir%03i.NC' % (L22_dict['emission_color'].upper(),
                                                        t_start.strftime('%Y-%m-%d_%H%M%S'),
                                                        data_version_major, data_revision)
    L22_full_fn = '%s%s'%(path, L22_fn)
    ncfile = netCDF4.Dataset(L22_full_fn,mode='w',format='NETCDF4') 
    
    try: # always close file if an error occurs
    
        ########################## Global Attributes #################################
        ncfile.Acknowledgement =       ''.join(("This is a data product from the NASA Ionospheric Connection Explorer mission, ",
                                                "an Explorer launched in June 2017.\n",
                                                "\n",
                                                "Responsibility of the mission science falls to the Principal Investigator, ",
                                                "Dr. Thomas Immel at UC Berkeley.\n",
                                                "\n",
                                                "Validation of the L1 data products falls to the instrument lead ",
                                                "investigators/scientists.\n",
                                                "  * EUV  Dr. Eric Korpela\n",
                                                "  * FUV  Dr. Harald Frey\n",
                                                "  * MIGHTI  Dr. Chris Englert\n",
                                                "  * IVM  Dr. Roderick Heelis\n",
                                                "\n",
                                                "Validation of the L2 data products falls to those responsible for those products.\n",
                                                "  * O/N2  Dr. Andrew Stephan\n",
                                                "  * Daytime (EUV) O+ profiles  Dr. Andrew Stephan\n",
                                                "  * Nighttime (FUV) O+ profiles  Dr. Farzad Kamalabadi\n",
                                                "  * Neutral Wind profiles  Dr. Jonathan Makela\n",
                                                "  * Neutral Temperature profiles  Dr. Chris Englert\n",
                                                "\n",
                                                "Responsibility for Level 4 products are detailed on the ICON website ",
                                                "(http://icon.ssl.berkeley.edu).\n",
                                                "\n",
                                                "Overall validation of the products is overseen by the ICON Project Scientist ",
                                                "Dr. Scott England.\n",
                                                "\n",
                                                "NASA oversight for all products is provided by the Mission Scientist ",
                                                "Dr. Douglas Rowland.\n",
                                                "\n",
                                                "Users of these data should contact and acknowledge the Principal Investigator ",
                                                "Dr. Immel and the party directly responsible for the data product and the NASA ",
                                                "Contract Number NNG12FA45C from the Explorers Project Office." ))

        ncfile.ADID_Ref =                       'NASA Contract > NNG12FA45C'
        ncfile.Calibration_File =               ''
        ncfile.Conventions =                    'SPDF ISTP/IACG Modified for NetCDF'
        ncfile.Data_Level =                     'L2.2'
        ncfile.Data_Type =                      'DP22 > Data Product 2.2: Cardinal Vector Winds'
        ncfile.Data_Version_Major =             np.uint16(data_version_major)
        ncfile.Data_Revision =                  np.uint16(data_revision)
        ncfile.Data_Version =                   data_version_major + 0.001 * data_revision
        ncfile.Date_Stop =                      t_stop.strftime('%a, %d %b %Y, %Y-%m-%dT%H:%M:%S.%f')[:-3] + ' UTC' 
        ncfile.Date_Start =                     t_start.strftime('%a, %d %b %Y, %Y-%m-%dT%H:%M:%S.%f')[:-3] + ' UTC' 
        ncfile.Description =                    'ICON MIGHTI Cardinal Vector Winds (DP 2.2)'
        ncfile.Descriptor =                     'MIGHTI > Michelson Interferometer for Global High-resolution Thermospheric Imaging' 
        ncfile.Discipline =                     'Space Physics > Ionospheric Science'
        ncfile.File =                           L22_fn
        ncfile.File_Date =                      t_file.strftime('%a, %d %b %Y, %Y-%m-%dT%H:%M:%S.%f')[:-3] + ' UTC'
        ncfile.Generated_By =                   'ICON SDC > ICON UIUC MIGHTI L2.2 Processor v%s, B. J. Harding (bhardin2@illinois.edu)' % __version__
        ncfile.Generation_Date =                t_file.strftime('%Y%m%d')
        ncfile.History =                        'Version %i, %s, %s, ' % (software_version_major, user_name, t_file.strftime('%Y-%m-%dT%H:%M:%S')) +\
                                                'MIGHTI L2.2 Processor v%s ' % __version__
        ncfile.HTTP_LINK =                      'http://icon.ssl.berkeley.edu/Instruments/MIGHTI'
        ncfile.Instrument =                     'MIGHTI'
        ncfile.Instrument_Type =                'Imagers (space)'
        ncfile.Link_Text =                      'MIGHTI Cardinal Vector Winds (DP 2.2)'
        ncfile.Link_Title =                     'ICON MIGHTI'
        ncfile.Logical_File_ID =                L22_fn[:-3]
        ncfile.Logical_Source =                 'ICON_L2_MIGHTI'
        ncfile.Logical_Source_Description =     'MIGHTI - Cardinal Vector Winds'
        ncfile.Mission_Group =                  'Ionospheric Investigations'
        ncfile.MODS =                           ncfile.History
        ncfile.Parents =                        parents
        ncfile.PI_Affiliation =                 'UC Berkeley > SSL'
        ncfile.PI_Name =                        'T. J. Immel'
        ncfile.Project =                        'NASA > ICON'
        ncfile.Rules_of_Use =                   'Public Data for Scientific Use'
        ncfile.Software_Version =               'ICON SDC > ICON UIUC MIGHTI L2.2 Processor v%s, B. J. Harding (bhardin2@illinois.edu)' % __version__
        ncfile.Source_Name =                    'ICON > Ionospheric Connection Explorer'
        ncfile.Spacecraft_ID =                  'NASA > ICON - 493'
        ncfile.Text =                           'ICON explores the boundary between Earth and space - the ionosphere - ' +\
                                                'to understand the physical connection between our world and the immediate '+\
                                                'space environment around us. Visit \'http://icon.ssl.berkeley.edu\' for more details.'
        ncfile.Text_Supplement =                'See Harding et al. [2017], doi:10.1007/s11214-017-0359-3'
        ncfile.Time_Resolution =                '30 or 60 seconds'
        ncfile.Title =                          'ICON MIGHTI Cardinal Vector Winds (DP 2.2)'


        ################################## Dimensions ########################################
        ny,nx = np.shape(L22_dict['alt'])
        ncfile.createDimension('Epoch',0)
        ncfile.createDimension('Altitude', ny)
        ncfile.createDimension('Longitude', nx)
        ncfile.createDimension('N_flags', np.shape(L22_dict['error_flags'])[2])
        


        ################################## Variables #########################################
        prefix = 'ICON_L2_2_MIGHTI_%s' % (L22_dict['emission_color'].upper())

        ######### Timing Variables #########

        # Time midpoint (the official required "Epoch" variable)
        # This is a little confusing since time is a dependent variable in our case, and the ISTP
        # format seems to want it to be the primary independent variable.
        t_msec = np.zeros((ny,nx),dtype=np.int64)
        t_fillval = np.int64(-1)
        for i in range(ny):
            for j in range(nx):
                if L22_dict['time'][i,j] is None:
                    t_msec[i,j] = t_fillval
                else:
                    t_msec[i,j] = np.int64(np.round((L22_dict['time'][i,j] - datetime(1970,1,1)).total_seconds()*1e3))
        var = _create_variable(ncfile, 'EPOCH', t_msec, 
                              dimensions=('Altitude', 'Longitude'),
                              format_nc='i8', format_fortran='I', desc='Sample time, midpoint of A and B measurements. Number of msec since Jan 1, 1970.', 
                              display_type='scalar', field_name='Time', fill_value=t_fillval, label_axis='Time', bin_location=0.5,
                              units='ms', valid_min=0, valid_max=1000*365*86400e3, var_type='support_data', chunk_sizes=[1,1],
                              notes='')
        
        # Also include a more human-readable, intuitive variable
        var = _create_variable(ncfile, '%s_TIME'%prefix, t_msec, 
                              dimensions=('Altitude', 'Longitude'),
                              format_nc='i8', format_fortran='I', desc='Sample time, midpoint of A and B measurements. Number of msec since Jan 1, 1970.', 
                              display_type='scalar', field_name='Time', fill_value=t_fillval, label_axis='Time', bin_location=0.5,
                              units='ms', valid_min=0, valid_max=1000*365*86400e3, var_type='support_data', chunk_sizes=[1,1],
                              notes='')

        
        ######### Data Location Variables #########
        
        # Altitude
        val = L22_dict['alt']*1e3 # convert to meters
        var_alt = _create_variable(ncfile, '%s_ALTITUDE'%prefix, val, 
                              dimensions=('Altitude','Longitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='WGS84 altitude of each wind sample', 
                              display_type='image', field_name='Altitude', fill_value=None, label_axis='', bin_location=0.5,
                              units='m', valid_min=0, valid_max=1e10, var_type='support_data', chunk_sizes=[1,1],
                              notes='')

        
        # Longitude
        var = _create_variable(ncfile, '%s_LONGITUDE'%prefix, L22_dict['lon'], 
                              dimensions=('Altitude','Longitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='WGS84 longitude of each wind sample', 
                              display_type='image', field_name='Longitude', fill_value=None, label_axis='', bin_location=0.5,
                              units='deg', valid_min=0., valid_max=360., var_type='support_data', chunk_sizes=[1,1],
                              notes='')
                              
        # Latitude
        var = _create_variable(ncfile, '%s_LATITUDE'%prefix, L22_dict['lat'], 
                              dimensions=('Altitude','Longitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='WGS84 latitude of each wind sample', 
                              display_type='image', field_name='Latitude', fill_value=None, label_axis='', bin_location=0.5,
                              units='deg', valid_min=-90., valid_max=90., var_type='support_data', chunk_sizes=[1,1],
                              notes='')
                              

        
        ######### Data Variables #########
        
        # Zonal Wind
        var = _create_variable(ncfile, '%s_ZONAL_WIND'%prefix, L22_dict['u'], 
                              dimensions=('Altitude','Longitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='Zonal component of the horizontal wind for an orbit. Positive Eastward.', 
                              display_type='image', field_name='Zonal Wind', fill_value=None, label_axis='', bin_location=0.5,
                              units='m/s', valid_min=-1e10, valid_max=1e10, var_type='data', chunk_sizes=[1,1],
                              notes='')
        
        # Meridional Wind
        var = _create_variable(ncfile, '%s_MERIDIONAL_WIND'%prefix, L22_dict['v'], 
                              dimensions=('Altitude','Longitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='Meridional component of the horizontal wind for an orbit. Positive Northward.', 
                              display_type='image', field_name='Meridional Wind', fill_value=None, label_axis='', bin_location=0.5,
                              units='m/s', valid_min=-1e10, valid_max=1e10, var_type='data', chunk_sizes=[1,1],
                              notes='')    
                              
                              
        # Zonal Wind Error
        var = _create_variable(ncfile, '%s_ZONAL_WIND_ERROR'%prefix, L22_dict['u_error'], 
                              dimensions=('Altitude','Longitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='Error in the zonal wind estimate.', 
                              display_type='image', field_name='Zonal Wind Error', fill_value=None, label_axis='', bin_location=0.5,
                              units='m/s', valid_min=-1e10, valid_max=1e10, var_type='data', chunk_sizes=[1,1],
                              notes='')
        
        # Meridional Wind Error
        var = _create_variable(ncfile, '%s_MERIDIONAL_WIND_ERROR'%prefix, L22_dict['v_error'], 
                              dimensions=('Altitude','Longitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='Error in the meridional wind estimate.', 
                              display_type='image', field_name='Meridional Wind Error', fill_value=None, label_axis='', bin_location=0.5,
                              units='m/s', valid_min=-1e10, valid_max=1e10, var_type='data', chunk_sizes=[1,1],
                              notes='')    


        ######### Other Metadata Variables #########
        
        # Fringe amplitude profile from MIGHTI-A
        var = _create_variable(ncfile, '%s_FRINGE_AMPLTIUDE_A'%prefix, L22_dict['ver_A'], 
                              dimensions=('Altitude','Longitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='Fringe Amplitude from MIGHTI-A', 
                              display_type='image', field_name='Fringe Amplitude A', fill_value=None, label_axis='', bin_location=0.5,
                              units='', valid_min=-1e30, valid_max=1e30, var_type='metadata', chunk_sizes=[1,1],
                              notes='The approximate volume emission rate (VER) in arbitrary units from MIGHTI A. ' +
                                    'Technically this is the visibility '+
                                    'of the fringes, which has a dependence on temperature and background emission.')
        
        # Fringe amplitude profile from MIGHTI-B
        var = _create_variable(ncfile, '%s_FRINGE_AMPLTIUDE_B'%prefix, L22_dict['ver_B'], 
                              dimensions=('Altitude','Longitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='Fringe Amplitude from MIGHTI-B', 
                              display_type='image', field_name='Fringe Amplitude A', fill_value=None, label_axis='', bin_location=0.5,
                              units='', valid_min=-1e30, valid_max=1e30, var_type='metadata', chunk_sizes=[1,1],
                              notes='The approximate volume emission rate (VER) in arbitrary units from MIGHTI B. ' +
                                    'Technically this is the visibility '+
                                    'of the fringes, which has a dependence on temperature and background emission.')
        
         # Fringe amplitude profile from MIGHTI-B
        var = _create_variable(ncfile, '%s_FRINGE_AMPLTIUDE'%prefix, L22_dict['ver'], 
                              dimensions=('Altitude','Longitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='Fringe Amplitude', 
                              display_type='image', field_name='Fringe Amplitude A', fill_value=None, label_axis='', bin_location=0.5,
                              units='', valid_min=-1e30, valid_max=1e30, var_type='metadata', chunk_sizes=[1,1],
                              notes='The approximate volume emission rate (VER) in arbitrary units, estimated by combining MIGHTI A and B. ' +
                                    'Technically this is the visibility '+
                                    'of the fringes, which has a dependence on temperature and background emission.')

        # Fringe amplitude relative difference
        var = _create_variable(ncfile, '%s_FRINGE_AMPLITUDE_RELATIVE_DIFFERENCE'%prefix, L22_dict['ver_rel_diff'], 
                              dimensions=('Altitude','Longitude'), # depend_0 = 'Altitude',
                              format_nc='f8', format_fortran='F', desc='Difference in MIGHTI A&B\'s fringe amplitude estimates, divided by the mean', 
                              display_type='image', field_name='Fringe Amplitude Difference', fill_value=None, label_axis='', bin_location=0.5,
                              units='', valid_min=-1e10, valid_max=1e10, var_type='metadata', chunk_sizes=[1,1],
                              notes='This is the quantity used to determine if spherical asymmetry flag is raised')    
        # Error flags
        
        var = _create_variable(ncfile, '%s_ERROR_FLAG'%prefix, L22_dict['error_flags'], 
                              dimensions=('Altitude','Longitude','N_flags'), # depend_0 = 'Altitude',
                              format_nc='b', format_fortran='I', desc='Error flags. See Var_Notes attribute for description.', 
                              display_type='image', field_name='Error Flags', fill_value=None, label_axis='', bin_location=0.5,
                              units='', valid_min=0, valid_max=1, var_type='metadata', chunk_sizes=[1,1,1],
                              notes='Ten error flags for each grid point, each either 0 or 1:\n' +\
                                    '    0 = missing MIGHTI A file'+\
                                    '    1 = missing MIGHTI B file'+\
                                    '    2 = A signal too weak'+\
                                    '    3 = B signal too weak'+\
                                    '    4 = A did not sample this altitude'+\
                                    '    5 = B did not sample this altitude'+\
                                    '    6 = A sample exists but is NaN'+\
                                    '    7 = B sample exists but is NaN'+\
                                    '    8 = Spherical asymmetry: A&B VER estimates disagree'+\
                                    '    9 = Unknown Error')
        
        ncfile.close()
        
    except: # Make sure the file is closed
        ncfile.close()
        raise
            
    return L22_full_fn




def level21_to_level22_without_info_file(L21_fns, L22_path, data_revision=0, sph_asym_thresh=None, time_start=None, time_stop=None):
    '''
    High-level function to apply the Level-2.1-to-Level-2.2 algorithm to a series of same-colored Level 2.1 files
    (in the L21_path directory) and create a single Level 2.2 file (in the L22_path directory). This version
    of the function requires the user to input parameters manually, instead of specifying an 
    Information.TXT file, like is done at the Science Data Center.
    
    INPUTS:
    
      *  L21_fns         -- TYPE:list of str.   A list of L2.1 files to be processed, including both MIGHTI-A and
                                                MIGHTI-B files, all from the same color.
      *  L22_path        -- TYPE:str.           The directory the L2.2 file will be saved in, including trailing "/"
                                                (e.g., '/home/user/')

    OPTIONAL INPUTS:
    
      *  data_revision   -- TYPE:int,      The minor version of the data [0-999]. The major version is set
                                           by the software's major version.
      *  sph_asym_thresh -- TYPE:float.    Relative difference in emission rate measured by A and B, beyond which
                                           the spherical-asymmetry flag will be raised. Technically, it should be
                                           "fringe amplitude" instead of "emission rate" due to the temperature
                                           dependence. Relative difference is defined as abs(A-B)/((A+B)/2). If 
                                           None (default), the default from MIGHTI_L2.global_params will be used.
      *  time_start      -- TYPE:datetime. A timezone-naive datetime in UT, specifying the beginning of the interval
                                           which the Level 2.2 data product should span. (Some L2.1 files from before
                                           the start time are needed to handle the crossover). If None (default), 
                                           the start time defaults to the first 0 UT after the first input file's time.
      *  time_stop       -- TYPE:datetime. A timezone-naive datetime in UT, specifying the end of the interval
                                           which the Level 2.2 data product should span. (Some L2.1 files from after
                                           the stop time are needed to handle the crossover). If None (default), 
                                           the stop time defaults to 24 hours after the start time. 
                                       
    OUTPUTS:
    
      *  L22_fn          -- TYPE:str.      The full path to the saved L2.2 file.
      
    '''
    
    assert len(L21_fns)>0, "No L2.1 files specified"
    

    ##### Load L2.1 files into dictionaries
    # Sort files by sensor
    Afns = [f for f in L21_fns if 'MIGHTI-A' in f]
    Bfns = [f for f in L21_fns if 'MIGHTI-B' in f]
    
    if len(Afns)==0:
        raise ValueError('No MIGHTI-A files found')
    if len(Bfns)==0:
        raise ValueError('No MIGHTI-B files found')

    # Sort the files by time (same as alphanumeric sorting)
    Afns.sort()
    Bfns.sort()

    # Read files (this is the bottleneck in terms of runtime)
    level21_A = level21_to_dict(Afns)
    level21_B = level21_to_dict(Bfns)

    ##### Run L2.2 processing to create L2.2 dictionary
    L22_dict = level21_dict_to_level22_dict(level21_A, level21_B, sph_asym_thresh=sph_asym_thresh,
                                                                  time_start=time_start, time_stop=time_stop)
    
    ##### Save L2.2 data to file
    L22_fn = save_nc_level22(L22_path, L22_dict, data_revision=data_revision)

    return L22_fn




def level21_to_level22(info_fn):
    '''
    Highest-level function to apply the Level-2.1-to-Level-2.2 algorithm. Inputs are specified via an information file.
    Files should be listed in the information file for a 24 hour period (0 UT to 0 UT), plus >15 minutes of files on either
    side. If files from green and red are both specified (as expected when run at the Science Data Center), they will be split
    and run separately by this function. The output Level 2.2 file(s) will be saved to the same directory
    as the input L2.1 files.
    
    INPUTS:
    
      * info_fn  -- TYPE:str.  Full path to an ASCII file in the following format:
      
                                        [PARAMETERS]
                                        Revision=001
                                        Directory=/path/to/wherever/
                                        <other parameters>

                                        [FILES]
                                        ICON_L2_MIGHTI-A_LINE-OF-SIGHT_WIND_GREEN_2009-03-23_23.30.00_v01r001.NC
                                        ICON_L2_MIGHTI-A_LINE-OF-SIGHT_WIND_GREEN_2009-03-23_23.31.00_v01r001.NC
                                        ICON_L2_MIGHTI-A_LINE-OF-SIGHT_WIND_GREEN_2009-03-23_23.32.00_v01r001.NC
                                        etc... including files from MIGHTI-B
                                      
    OUTPUTS:
    
      *  ret     -- TYPE:str. '0' if everything worked. If not, a human-readable error message for each file that failed
      
    '''
    
    info, L21_fns = read_info_file(info_fn)
    L21_fns.sort()
    
    # Parse the info
    # (0) Make sure there's a trailing "/" on the directory
    direc = info['Directory']
    if direc[-1] != '/':
        direc += '/'
    # (1) Add the directory to all the L2.1 files
    L21_full_fns = []
    for L21_fn in L21_fns:
        L21_full_fns.append(direc + L21_fn)
    # (2) Parse list of data revision numbers
    s = info['Revision'].split(',')
    data_revision = [int(x) for x in s]
    # For L2.2, we only expect a single revision number
    assert len(data_revision)==1, "Multiple revision numbers not supported for Level 2.2 processing"
    data_revision = data_revision[0]
    
    # For both red and green, call lower-level function which does all the real work
    L22_fns = []
    failure_messages = []
    for emission_color in ['red','green']:
        # Extract L2.1 files with this color
        L21_fns_color = [fn for fn in L21_fns if emission_color.upper() in fn]
        try:
            L22_fn = level21_to_level22_without_info_file(L21_fns_color, direc, data_revision=data_revision)       
            L22_fns.append(L22_fn)
        except Exception as e:
            failure_messages.append('Failed processing:\n\tColor   = %s\%s\n'%(emission_color,traceback.format_exc()))
                
                
    if not failure_messages: # Everything worked
        return '0' # Is this what Tori wants?
    
    else:
        s = '\n'.join(failure_messages)
        print(s)
        return s




################################################################################################################
#############################################    Unit Tests    #################################################
################################################################################################################


def _test_level1_to_level21():
    '''
    Special-purpose function to test the installation of the Level 1 to Level 2.1 processing code. This assumes that 
    the user is in the directory containing the test data.
    '''

    L21_old_fns = ['ICON_L2_MIGHTI-A_LINE-OF-SIGHT_WIND_GREEN_2017-05-29_011133_v00r001_old.NC',
                   'ICON_L2_MIGHTI-A_LINE-OF-SIGHT_WIND_RED_2017-05-29_011133_v00r001_old.NC',]

    variable_stubs =    ['TIME',
                         'ALTITUDE',
                         'LATITUDE',
                         'LONGITUDE',
                         'LINE_OF_SIGHT_AZIMUTH',
                         'LINE_OF_SIGHT_WIND',
                         'LINE_OF_SIGHT_WIND_ERROR',
                         'FRINGE_AMPLTIUDE',
                         'FRINGE_AMPLTIUDE_ERROR',
                         'SPACECRAFT_VELOCITY_VECTOR',
                         'SPACECRAFT_LATITUDE',
                         'SPACECRAFT_LONGITUDE',
                         'SPACECRAFT_ALTITUDE',
                         'LINE_OF_SIGHT_VECTOR',]

    print '\nMIGHTI L1-L2.1 PROCESSING v%s\n' % (__version__)
    
    print 'Processing L1 file...'
    ret = level1_to_level21('L21_info.txt')
    print 'Complete\n'
    
    # Find new files that were created 
    L21_fns = glob.glob('./ICON_L2_*r001.NC')
    L21_fns.sort()
    assert L21_fns, "No output data files found."
    
    for L21_fn, L21_old_fn in zip(L21_fns, L21_old_fns):
    
        print 'Comparing new file: %s\nWith old file:      ./%s\n' % (L21_fn, L21_old_fn)
        
        emission_color = L21_fn.split('_')[5]
        prefix = 'ICON_L2_1_MIGHTI_A_%s_' % (emission_color)

        d0 = netCDF4.Dataset(L21_fn)
        d1 = netCDF4.Dataset(L21_old_fn)

        print '%60s:  Difference' % ('Variable')
        for stub in variable_stubs:
            v = prefix + stub
            v0 = d0.variables[v][...]
            v1 = d1.variables[v][...]
            e = np.linalg.norm(v1-v0)
            print '%60s:  %e %s' % (v,e, d0.variables[v].Units)

        d0.close()
        d1.close()

        print ''


        
    
    
def _test_level21_to_level22():
    '''
    Special-purpose function to test the installation of the Level 2.1 to Level 2.2 processing code. This assumes that 
    the user is in the directory containing the test data. Here we just test green, since red is identical. Both
    red and green are tested in L2.1.
    '''
    
    L22_old_fn = 'ICON_L2_2_MIGHTI_VECTOR_WIND_GREEN_2009-03-24_00.00.00_v00r000.NC'    
    
    print '\nMIGHTI L2.1-L2.2 PROCESSING v%s\n' % (__version__)
    print 'Reading L22_info.txt and running L2.1-to-L2.2 code. Red will fail and Green will succeed.'
    print 'This will take a few minutes....\n'
    # Run processing on test data
    msg = level21_to_level22('L22_info.txt')
    print 'Processing complete\n'
    
    # Find the new file that was created
    L22_fns = glob.glob('./ICON_L2_MIGHTI_VECTOR*.NC') # This may (should) contain the old file as well as the new
    if L22_old_fn in L22_fns:
        L22_fns.remove(L22_old_fn)
    assert len(L22_fns)==1, "Too many L2.2 files found"
    L22_fn = L22_fns[0]
    
    
    print 'Comparing new file: %s\nWith old file:      ./%s\n' % (L22_fn, L22_old_fn)
    variables = ['EPOCH',
                 'ICON_L2_2_MIGHTI_GREEN_TIME',
                 'ICON_L2_2_MIGHTI_GREEN_ALTITUDE',
                 'ICON_L2_2_MIGHTI_GREEN_LONGITUDE',
                 'ICON_L2_2_MIGHTI_GREEN_LATITUDE',
                 'ICON_L2_2_MIGHTI_GREEN_ZONAL_WIND',
                 'ICON_L2_2_MIGHTI_GREEN_MERIDIONAL_WIND',
                 'ICON_L2_2_MIGHTI_GREEN_ZONAL_WIND_ERROR',
                 'ICON_L2_2_MIGHTI_GREEN_MERIDIONAL_WIND_ERROR',
                 'ICON_L2_2_MIGHTI_GREEN_FRINGE_AMPLITUDE_RELATIVE_DIFFERENCE',
                 'ICON_L2_2_MIGHTI_GREEN_ERROR_FLAG']

    d0 = netCDF4.Dataset(L22_fn)
    d1 = netCDF4.Dataset(L22_old_fn)

    print '%60s:  Difference' % ('Variable')
    for v in variables:
        v0 = d0.variables[v][...]
        v1 = d1.variables[v][...]
        e =  np.linalg.norm(v1-v0)
        print '%60s:  %e %s' % (v,e, d0.variables[v].Units)
    d0.close()
    d1.close()


    
    
    
    
    
