#!/usr/bin/env python3
import argparse
import importlib
import os
import os.path as op
import pickle
import struct
import time
import warnings
from uuid import uuid4

import dash_core_components as dcc
import dash_html_components as html
import nibabel as nib
import numpy as np
import redis
import yaml

import cortex
from realtimefmri import buffered_array, config, image_utils, pipeline_utils
from realtimefmri.utils import get_logger

logger = get_logger('preprocess', to_console=True, to_network=True)
r = redis.StrictRedis(config.REDIS_HOST)


def preprocess(recording_id, pipeline_name, surface, transform, **kwargs):
    """Highest-level class for running preprocessing

    This class loads the preprocessing pipeline from the configuration
    file, initializes the classes for each step, and runs the main loop
    that receives incoming images from the data collector.

    Parameters
    ----------
    pipeline_name : str
        Name of preprocessing configuration to use. Should be a file in the
        `pipeline` filestore
    recording_id : str
        A unique identifier for the recording
    log : bool
        Whether to send log messages to the network logger
    verbose : bool
        Whether to log to the console
    """
    config_path = op.join(config.PIPELINE_DIR, pipeline_name + '.yaml')
    with open(config_path, 'rb') as f:
        pipeline_config = yaml.load(f)

    pipeline_config['global_parameters']['surface'] = surface
    pipeline_config['global_parameters']['transform'] = transform

    pipeline = Pipeline(**pipeline_config)

    n_skip = pipeline.global_parameters.get('n_skip', 0)

    volume_subscription = r.pubsub()
    volume_subscription.subscribe('timestamped_volume')
    for message in volume_subscription.listen():
        if message['type'] == 'message':
            data = message['data']
            if data != 1:  # subscription message
                timestamped_volume = pickle.loads(data)
                logger.info('Received image %d', timestamped_volume['image_number'])
                data_dict = {'image_number': timestamped_volume['image_number'],
                             'raw_image_nii': timestamped_volume['volume']}
                data_dict = pipeline.process(data_dict)


class Pipeline():
    """Construct and run a preprocessing pipeline

    Load a preprocessing configuration file, intialize all of the steps, and
    process each image through the pipeline.

    Parameters
    ----------
    pipeline : list of dict
        The parameters for pipeline steps
    global_parameters : dict
        Settings passed as keyword arguments to each pipeline step
    static_pipeline : list of dict
        Pipeline steps that start with initialization and do not receive input
    recording_id : str
        A unique identifier for the recording. If none is provided, one will be
        generated from the subject name and date
    log : bool
        Log to network logger
    verbose : bool
        Log to console

    Attributes
    ----------
    global_parameters : dict
        Dictionary of arguments that are sent as keyword arguments to every
        preprocessing step. Useful for values that are required in multiple
        steps like recording identifier, subject name, and transform name
    static_pipeline : list
        List of dictionaries that configure initialization steps. These are run
        once at the outset of the program.
    pipeline : list
        List of dictionaries that configure steps in the pipeline. These are
        run for each image that arrives at the pipeline.
    log : logging.Logger
        The logger object

    Methods
    -------
    process(data_dict)
        Run the data in ```data_dict``` through each of the preprocessing steps
    """
    def __init__(self, pipeline, global_parameters={}, static_pipeline={}, recording_id=None):
        if recording_id is None:
            recording_id = 'recording_{}'.format(time.strftime('%Y%m%d_%H%M'))

        self.recording_id = recording_id
        self.build(pipeline, static_pipeline, global_parameters)
        self.register()

    def build(self, pipeline, static_pipeline, global_parameters):
        """Build the pipeline from the pipeline parameters. Directly sets the class instance
        attributes for `pipeline`, `static_pipeline`, and `global_parameters`

        Parameters
        ----------
        pipeline : list of dicts
        static_pipeline : list of dicts
        global_parameters : dict
        """
        self.static_pipeline = []
        for step in static_pipeline:
            logger.debug('Initializing %s', step['name'])
            args = step.get('args', ())
            kwargs = step.get('kwargs', {})
            for k, v in global_parameters.items():
                kwargs[k] = kwargs.get(k, v)

            cls = pipeline_utils.load_class(step['class_name'])
            step['instance'] = cls(*args, **kwargs)
            self.static_pipeline.append(step)

        self.pipeline = []
        for step in pipeline:
            logger.debug('initializing %s', step['name'])
            args = step.get('args', ())
            kwargs = step.get('kwargs', dict())
            for k, v in global_parameters.items():
                kwargs[k] = kwargs.get(k, v)

            cls = pipeline_utils.load_class(step['class_name'])
            step['instance'] = cls(*args, **kwargs)
            self.pipeline.append(step)

        self.global_parameters = global_parameters

    @classmethod
    def load_from_saved_pipelines(cls, pipeline_name, **kwargs):
        """Load from the pipelines stored with the pacakge

        Parameters
        ----------
        pipeline_name : str
            The name of a pipeline stored in the pipeline directory

        Returns
        -------
        A Pipeline class for the specified pipeline name
        """
        config_path = op.join(config.PIPELINE_DIR, pipeline_name + '.yaml')
        return cls.load_from_config(config_path, **kwargs)

    @classmethod
    def load_from_config(cls, config_path, **kwargs):
        with open(config_path, 'rb') as f:
            config = yaml.load(f)

        kwargs.update(config)
        return cls(**kwargs)

    def process(self, data_dict):
        """Run through the preprocessing steps

        Iterate through all the preprocessing steps. For each step, extract the `input` keys from
        the `data_dict` ans pass them as ordered unnamed arguments to that step. The return value
        is saved to the `data_dict` using the  `output` key.

        Parameters
        ----------
        data_dict : dict
            A dictionary containing all the processing results


        Returns
        -------
        A dictionary of all processing results
        """
        image_number = struct.pack('i', data_dict['image_number'])
        for step in self.pipeline:
            inputs = [data_dict[k] for k in step['input']]

            logger.info('running %s', step['name'])
            outp = step['instance'].run(*inputs)

            logger.debug('finished %s', step['name'])

            if not isinstance(outp, (list, tuple)):
                outp = [outp]

            d = dict(zip(step.get('output', []), outp))
            data_dict.update(d)

        return data_dict

    @staticmethod
    def create_interface(key):
        contents = []
        for class_name_key in r.scan_iter(key + b':*:class_name'):
            class_name = pickle.loads(r.get(class_name_key))
            step_index = int(class_name_key.split(b':')[2].decode('utf-8'))
            step_class = pipeline_utils.load_class(class_name)
            step_key = class_name_key.rsplit(b':', maxsplit=1)[0]
            interface = step_class.interface(step_key)
            contents.append([step_index, interface])

        contents = sorted(contents, key=lambda x: x[0])
        contents = [content for i, content in contents]

        return contents

    def register(self):
        """Register the pipeline to the redis database
        """
        pipeline_key = f'pipeline:{id(self)}'
        for step_index, step in enumerate(self.pipeline):
            step_key = f'{pipeline_key}:{step_index}'
            step['instance'].register(step_key)

        self._key = pipeline_key


class PreprocessingStep(object):
    """Preprocessing step
    """
    def __init__(self, *args, **kwargs):
        """
        """
        self._parameters = kwargs

    def register(self, key):
        """Register the preprocessing step to the redis database

        Parameters
        ----------
        key : str
            A unique key for this step. Convention is pipeline:<pipeline_id>:<step_index>,
            e.g., pipeline:105874924:0, pipeline:105874924:1, pipeline:105874924:2, etc.
        """
        r.set(key + ':class_name', pickle.dumps(pipeline_utils.get_step_name(self.__class__)))

        for k, v in self._parameters.items():
            r.set(key + f':{k}', pickle.dumps(v))

        self._key = key

    @staticmethod
    def interface(step_key):
        """Define an interface element for the control panel
        """
        step_id = step_key.decode('utf-8').replace(':', '-')

        step = {}
        for key in r.scan_iter(step_key + b':*'):
            param_name = key.rsplit(b':', maxsplit=1)[1]
            val = r.get(key)
            step[param_name] = pickle.loads(val)

        name = step.pop(b'class_name')
        contents = [html.H3(name)]
        for k, v in step.items():
            k = k.decode('utf-8')
            contents.extend([html.Strong(k),
                             dcc.Input(value=v, id=f'pipeline-{step_id}-{k}')])

        interface = html.Div(contents, id=f'pipeline-{step_id}')
        logger.debug(interface)
        return interface

    def run(self):
        raise NotImplementedError


class Debug(PreprocessingStep):
    def run(self, nii):
        return str(nii), nii.shape


class SaveNifti(PreprocessingStep):
    """Saves nifti images to files

    Creates a subfolder in the recording directory and saves each incoming
    image as a nifti file.

    Parameters
    ----------
    recording_id : str
        Unique identifier for the run
    path_format : str
        Filename formatting string that is compatible with "%" string
        formatting. Must be able to format an integer containing the TR number.

    Attributes
    ----------
    recording_id : str
        Unique identifier for the run
    path_format : str
        Filename formatting string that is compatible with "%" string
        formatting. Must be able to format an integer containing the TR number.

    Methods
    --------
    run(inp)
        Saves the input image to a file and iterates the counter.
    """

    def __init__(self, recording_id=None, path_format='volume_{:04}.nii', **kwargs):
        parameters = {'recording_id': recording_id, 'path_format': path_format}
        parameters.update(kwargs)
        super(SaveNifti, self).__init__(**parameters)

        if recording_id is None:
            recording_id = str(uuid4())
        recording_dir = op.join(config.RECORDING_DIR, recording_id, 'nifti')
        try:
            os.makedirs(recording_dir)
        except OSError:
            pass

        print(recording_dir)
        self.recording_dir = recording_dir
        self.path_format = path_format

    def run(self, inp, image_number):
        path = self.path_format.format(image_number)
        nib.save(inp, op.join(self.recording_dir, path))
        print('saving to {}'.format(op.join(self.recording_dir, path)))


class MotionCorrect(PreprocessingStep):
    """Motion corrects images to a reference image

    Uses AFNI ``3dvolreg`` to motion correct the incoming images to a reference
    image stored in the pycortex database.

    Parameters
    ----------
    surface : str
        surface name in pycortex filestore
    transform : str
        Transform name for the surface in pycortex filestore

    Attributes
    ----------
    reference_affine : numpy.ndarray
        Affine transform for the reference image
    reference_path : str
        Path to the reference image

    Methods
    -------
    run(input_volume)
        Motion corrects the incoming image to the provided reference image and
        returns the motion corrected volume
    """
    def __init__(self, surface, transform, twopass=False, *args, **kwargs):
        parameters = {'surface': surface, 'transform': transform, 'twopass': twopass}
        parameters.update(kwargs)
        super(MotionCorrect, self).__init__(**parameters)
        ref_path = op.join(cortex.database.default_filestore,
                           surface, 'transforms', transform,
                           'reference.nii.gz')

        nii = nib.load(ref_path)
        self.reference_affine = nii.affine
        self.reference_path = ref_path
        self.twopass = twopass
        print(ref_path)

    def run(self, input_volume):
        same_affine = np.allclose(input_volume.affine[:3, :3],
                                  self.reference_affine[:3, :3])
        if not same_affine:
            print(input_volume.affine)
            print(self.reference_affine)
            warnings.warn('Input and reference volumes have different affines.')

        return image_utils.register(input_volume, self.reference_path, twopass=self.twopass)


class NiftiToVolume(PreprocessingStep):
    """Extract data volume from Nifti image. Translates image dimensions to be consistent with
    pycortex convention, e.g., volume shape is (30, 100, 100)
    """
    def run(self, nii):
        return nii.get_data().T


class VolumeToMosaic(PreprocessingStep):
    def __init__(self, dim=0, *args, **kwargs):
        parameters = {'dim': dim}
        parameters.update(kwargs)
        super(VolumeToMosaic, self).__init__(**parameters)
        self.dim = dim

    def run(self, volume):
        return cortex.mosaic(volume, dim=self.dim, show=False)[0]


class ApplyMask(PreprocessingStep):
    """Apply a voxel mask from the pycortex database to a volume

    Parameters
    ----------
    surface : str
        Subject name
    transform : str
        Pycortex transform name
    mask_type : str
        Type of mask

    Attributes
    ----------
    mask : numpy.ndarray
        Boolean voxel mask
    """
    def __init__(self, surface, transform, mask_type=None, *args, **kwargs):
        parameters = {'surface': surface, 'transform': transform, 'mask_type': mask_type}
        parameters.update(kwargs)
        super(ApplyMask, self).__init__(**parameters)
        mask = cortex.db.get_mask(surface, transform, mask_type)
        self.mask = mask

    def run(self, volume):
        """Apply the mask to a volume

        Parameters
        -----------
        volume : array
        """
        return volume[self.mask]


class ArrayMean(PreprocessingStep):
    """Compute the mean of an array

    Parameters
    ----------
    dimensions : tuple of int
        Dimensions along which to take the mean. None takes the mean of all values in the array
    """
    def __init__(self, dimensions, *args, **kwargs):
        parameters = {'dimensions': dimensions}
        parameters.update(kwargs)
        super(ArrayMean, self).__init__(**parameters)
        self.dimensions = tuple(dimensions)

    def run(self, array):
        """Take the mean of the array along the specified dimensions

        Parameters
        -----------
        array : array
        """
        if self.dimensions is None:
            return np.mean(array)
        else:
            return np.mean(array, axis=self.dimensions)


class ApplySecondaryMask(PreprocessingStep):
    """Apply a second mask to a vector produced by a first mask.

    Given a vector of voxel activity from a primary mask, return voxel activity
    for a secondary mask. Both masks are 3D voxel masks and resulting vector
    will be as if the intersection of primary and secondary masks was applied
    to the original 3D volume.

    Parameters
    ----------
    surface : str
        Subject name
    transform : str
        Pycortex transform name
    mask_type_1 : str
        Mask type for initial mask. Incoming vector results from applying this
        mask to the 3D volume
    mask_type_2 : str
        Mask type for secondary mask.

    Attributes
    ----------
    mask : numpy.ndarray
       A boolean vector that selects elements from the vector output of primary
       mask applied to a volume that are also in secondary mask.

    Methods
    -------
    run(x)
        Returns a vector of voxel activity of the intersection between primary
        and secondary masks
    """
    def __init__(self, surface, transform, mask_type_1, mask_type_2, **kwargs):
        parameters = {'surface': surface, 'transform': transform,
                      'mask_type_1': mask_type_1, 'mask_type_2': mask_type_2}
        parameters.update(kwargs)
        super(ApplySecondaryMask, self).__init__(**parameters)
        mask1 = cortex.db.get_mask(surface, transform, mask_type_1).T  # in xyz
        mask2 = cortex.db.get_mask(surface, transform, mask_type_2).T  # in xyz
        self.mask = image_utils.secondary_mask(mask1, mask2, order='F')

    def run(self, x):
        if x.ndim > 1:
            x = x.reshape(-1, 1)
        return x[self.mask]


class ActivityRatio(PreprocessingStep):
    def __init__(self, *args, **kwargs):
        super(ActivityRatio, self).__init__(**kwargs)

    def run(self, x1, x2):
        if isinstance(x1, np.ndarray):
            x1 = np.nanmean(x1)
        if isinstance(x2, np.ndarray):
            x2 = np.nanmean(x2)

        return x1 / (x1 + x2)


class RoiActivity(PreprocessingStep):
    """Extract activity from an ROI.

    Placeholder

    Parameters
    ----------
    surface : str
        Subject name
    transform : str
        pycortex transform ID
    pre_mask_name : str
        ROI masks returned by pycortex are in volume space, but activity is
        provided as a vector of gray matter activity. ``pre_mask_name`` is the
        name of the mask that was applied to the raw image volume to produce
        the gray matter activity vector.
    roi_names : list of str
        names of the ROIs to extract

    Attributes
    ----------
    masks : dict
        A dictionary containing the voxel masks for each named ROI

    Methods
    -------
    run():
        Returns a list of floats of mean activity in the requested ROIs
    """
    def __init__(self, surface, transform, pre_mask_name, roi_names, *args, **kwargs):
        parameters = {'surface': surface, 'transform': transform,
                      'pre_mask_name': pre_mask_name, 'roi_names': roi_names}
        parameters.update(kwargs)
        super(RoiActivity, self).__init__(**parameters)

        subj_dir = config.get_subject_directory(surface)
        pre_mask_path = op.join(subj_dir, pre_mask_name + '.nii')

        # mask in zyx
        pre_mask = nib.load(pre_mask_path).get_data().T.astype(bool)

        # returns masks in zyx
        roi_masks, roi_dict = cortex.get_roi_masks(surface, transform, roi_names)

        self.masks = dict()
        for name, mask_value in roi_dict.items():
            roi_mask = roi_masks == mask_value
            self.masks[name] = image_utils.secondary_mask(pre_mask, roi_mask)

    def run(self, activity):
        if activity.ndim > 1:
            activity = activity.reshape(-1, 1)
        roi_activities = dict()
        for name, mask in self.masks.items():
            roi_activities[name] = float(activity[mask].mean())
        return roi_activities


class WMDetrend(PreprocessingStep):
    """Detrend a volume using white matter detrending

    Uses a pre-trained white matter detrender to remove the trend from a
    volume.

    Parameters
    ----------
    subject : str
        Subject identifier
    model_name : str
        Name of the pre-trained white matter detrending model

    Attributes
    ----------
    subject : str
        Subject identifier
    model_name : str
        Name of white matter detrending model in subject's directory

    Methods
    -------
    run(wm_activity, gm_activity)
        Returns detrended grey matter activity given raw gray and white matter
        activity
    """
    def __init__(self, subject, model_name=None, *args, **kwargs):
        parameters = {'subject': subject, 'model_name': model_name}
        parameters.update(kwargs)
        super(WMDetrend, self).__init__(**parameters)
        subj_dir = config.get_subject_directory(subject)

        model_path = op.join(subj_dir, 'model-%s.pkl' % model_name)
        pca_path = op.join(subj_dir, 'pca-%s.pkl' % model_name)

        with open(model_path, 'r') as f:
            model = pickle.load(f)

        with open(pca_path, 'r') as f:
            pca = pickle.load(f)

        self.model = model
        self.pca = pca

    def run(self, wm_activity, gm_activity):
        wm_activity_pcs = self.pca.transform(wm_activity.reshape(1, -1)).reshape(1, -1)
        gm_trend = self.model.predict(wm_activity_pcs)
        return gm_activity - gm_trend


class IncrementalMeanStd(PreprocessingStep):
    """Preprocessing module that z-scores data using running mean and variance
    """
    def run(self, array):
        """Run the z-scoring on one time point and update the prior

        Parameters
        ----------
        array : numpy.ndarray
            A vector of data to be z-scored

        Returns
        -------
        The input array z-scored using the posterior mean and variance
        """
        if not hasattr(self, 'data'):
            self.array_shape = array.shape
            self.data = buffered_array.BufferedArray(array.size, dtype=array.dtype)
            self.data.append(array.ravel())
            return None, None

        self.data.append(array.ravel())

        std = np.std(self.data.get_array(), 0)
        mean = np.mean(self.data.get_array(), 0)

        return mean.reshape(self.array_shape), std.reshape(self.array_shape)

    def reset(self):
        del self.data


class RunningMeanStd(PreprocessingStep):
    """Compute a running mean and standard deviation for a set of voxels

    Compute a running mean and standard deviation, looking back a set number of
    samples.

    Parameters
    ----------
    n : int
        The number of past samples over which to compute mean and standard
        deviation

    Attributes
    ----------
    n : int
        The number of past samples over which to compute mean and standard
        deviation
    mean : numpy.ndarray
        The mean for the samples
    std : numpy.ndarray
        The standard deviation for the samples
    samples : numpy.ndarray
        The stored samples

    Methods
    -------
    run(inp)
        Adds the input vector to the stored samples (discard the oldest sample)
        and compute and return the mean and standard deviation.
    """
    def __init__(self, n=20, n_skip=5, *args, **kwargs):
        parameters = {'n': n, 'n_skip': n_skip}
        parameters.update(kwargs)
        super(RunningMeanStd, self).__init__(**parameters)
        self.n = n
        self.mean = None
        self.samples = None
        self.n_skip = n_skip

    def run(self, inp, image_number=None):
        if image_number < self.n_skip:
            return np.zeros(inp.size), np.ones(inp.size)

        if self.mean is None:
            self.samples = np.empty((self.n, inp.size)) * np.nan
        else:
            self.samples[:-1, :] = self.samples[1:, :]

        self.samples[-1, :] = inp
        self.mean = np.nanmean(self.samples, 0)
        self.std = np.nanstd(self.samples, 0)
        return self.mean, self.std


class ZScore(PreprocessingStep):
    """Compute a z-scored version of an input array given precomputed means and standard deviations

    Methods
    -------
    run(inp, mean, std)
        Return the z-scored version of the data
    """
    def run(self, array, mean, std):
        if mean is None:
            return np.zeros_like(array)
        else:
            return (array - mean) / std
