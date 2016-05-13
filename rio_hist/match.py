from __future__ import division, absolute_import
import logging
import os

import numpy as np
import rasterio
from .utils import cs_forward, cs_backward
from .plot import make_plot

logger = logging.getLogger(__name__)


def histogram_match(source, reference, plot_name=None):
    """
    Adjust the values of a source array
    so that its histogram matches that of a reference array

    Parameters:
    -----------
        source: np.ndarray
        reference: np.ndarray

    Returns:
    -----------
        target: np.ndarray
            The output array with the same shape as source
            but adjusted so that its histogram matches the reference
    """
    orig_shape = source.shape
    source = source.ravel()

    if np.ma.is_masked(reference):
        logger.debug("ref is masked, compressing")
        reference = reference.compressed()
    else:
        logger.debug("ref is unmasked, raveling")
        reference = reference.ravel()

    # get the set of unique pixel values
    # and their corresponding indices and counts
    logger.debug("Get unique pixel values")
    s_values, s_idx, s_counts = np.unique(
        source, return_inverse=True, return_counts=True)
    r_values, r_counts = np.unique(reference, return_counts=True)
    s_size = source.size

    if np.ma.is_masked(source):
        logger.debug("source is masked; get mask_index and remove masked values")
        mask_index = np.ma.where(s_values.mask)
        s_size = np.ma.where(s_idx != mask_index[0])[0].size
        s_values = s_values.compressed()
        s_counts = np.delete(s_counts, mask_index)

    # take the cumsum of the counts; empirical cumulative distribution
    logger.debug("calculate cumulative distribution")
    s_quantiles = np.cumsum(s_counts).astype(np.float64) / s_size
    r_quantiles = np.cumsum(r_counts).astype(np.float64) / reference.size

    # find values in the reference corresponding to the quantiles in the source
    logger.debug("interpolate values from source to reference by cdf")
    interp_r_values = np.interp(s_quantiles, r_quantiles, r_values)

    if np.ma.is_masked(source):
        logger.debug("source is masked, add fill_value back at mask_index")
        interp_r_values = np.insert(interp_r_values, mask_index[0], source.fill_value)

    # using the inverted source indicies, pull out the interpolated pixel values
    logger.debug("create target array from interpolated values by index")
    target = interp_r_values[s_idx]

    if np.ma.is_masked(source):
        logger.debug("source is masked, remask those pixels by position index")
        target = np.ma.masked_where(s_idx == mask_index[0], target)
        target.fill_value = source.fill_value

    return target.reshape(orig_shape)


def calculate_mask(arr):
    msk = arr.mask
    if msk.sum() == 0:
        mask = None
        fill = None
    else:
        # TODO alpha band? gdal mask? union of 3 bands?
        # IOW how to get the definitive 2D mask?
        mask = (msk[0] & msk[1] & msk[2])
        fill = arr.fill_value
    return mask, fill


def hist_match_worker(src_path, ref_path, dst_path,
                      creation_options, bands, color_space, plot):
    """Match histogram of src to ref, outputing to dst
    optionally output a plot to <dst>.jpg
    """
    logger.info("Matching {} to histogram of {} using {} color space".format(
        os.path.basename(src_path), os.path.basename(ref_path), color_space))

    with rasterio.open(src_path) as src:
        profile = src.profile.copy()
        src_arr = src.read(masked=True)
        src_mask, src_fill = calculate_mask(src_arr)
        src_arr = src_arr.filled()

    with rasterio.open(ref_path) as ref:
        ref_arr = ref.read(masked=True)
        ref_mask, ref_fill = calculate_mask(ref_arr)
        ref_arr = ref_arr.filled()

    src = cs_forward(src_arr, color_space)
    ref = cs_forward(ref_arr, color_space)

    bixs = tuple([int(x) - 1 for x in bands.split(',')])
    band_names = [color_space[x] for x in bixs]  # assume 1 letter per band

    target = src.copy()
    for i, b in enumerate(bixs):
        logger.debug("Processing band {}".format(b))
        src_band = src[b]
        ref_band = ref[b]

        # Re-apply 2D mask to each band
        if src_mask is not None:
            logger.debug("apply src_mask to band {}".format(b))
            src_band = np.ma.asarray(src_band)
            src_band.mask = src_mask
            src_band.fill_value = src_fill

        if ref_mask is not None:
            logger.debug("apply ref_mask to band {}".format(b))
            ref_band = np.ma.asarray(ref_band)
            ref_band.mask = ref_mask
            ref_band.fill_value = ref_fill

        target[b] = histogram_match(src_band, ref_band)

    target_rgb = cs_backward(target, color_space)

    # re-apply src_mask to target_rgb and write ndv
    if src_mask is not None:
        logger.debug("apply src_mask to target_rgb")
        if not np.ma.is_masked(target_rgb):
            target_rgb = np.ma.asarray(target_rgb)
        target_rgb.mask = np.array((src_mask, src_mask, src_mask))
        target_rgb.fill_value = src_fill

    profile['dtype'] = 'uint8'
    profile['transform'] = profile['affine']
    profile.update(creation_options)

    logger.info("Writing raster {}".format(dst_path))
    with rasterio.open(dst_path, 'w', **profile) as dst:
        dst.write(target_rgb)

    if plot:
        outplot = os.path.splitext(dst_path)[0] + "_plot.jpg"
        logger.info("Writing figure to {}".format(outplot))
        make_plot(
            src_path, ref_path, dst_path,
            src, ref, target,
            output=outplot,
            bands=tuple(zip(bixs, band_names)))
