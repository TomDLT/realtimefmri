#!/usr/bin/env python3
import os
import os.path as op
import struct
import tempfile
import subprocess
import nibabel
import pickle
import redis
from realtimefmri.utils import get_logger
from realtimefmri import config


def dicom_to_nifti(dicom_path):
    """Convert dicom image to nibabel nifti

    Parameters
    ----------
    dicom_path : str
        Path to dicom image

    Returns
    -------
    A nibabel.nifti1.Nifti1Image
    """
    d = tempfile.TemporaryDirectory()
    cmd = ['dcm2niix',
           '-s', 'y',
           '-b', 'n',
           '-1',
           '-o', d.name, dicom_path]

    subprocess.check_call(cmd)
    nii = nibabel.load(op.join(d.name, os.listdir(d.name)[0]), mmap=False)
    _ = nii.get_data()
    d.cleanup()

    return nii


def collect(verbose=True):
    """Continuously monitor for incoming volumes, merge with TTL timestamps, and send to 
    preprocessor
    """
    logger = get_logger('collector', to_console=verbose, to_network=True)
    logger.info('data collector initialized')

    redis_client = redis.StrictRedis(config.REDIS_HOST)
    volume_subscriber = redis_client.pubsub()
    volume_subscriber.subscribe('volume')

    for image_number, message in enumerate(volume_subscriber.listen()):
        if message['type'] == 'message':
            new_volume_path = message['data'].decode('utf8')
            logger.info('New volume {}'.format(new_volume_path))
            timestamp = redis_client.rpop('timestamp')
            timestamp = struct.unpack('d', timestamp)[0]
            logger.info('Collected at {}'.format(timestamp))

            nii = dicom_to_nifti(new_volume_path)

            logger.debug('%s %s', op.basename(new_volume_path), str(nii.shape))
            redis_client.publish('timestamped_volume', pickle.dumps([image_number, timestamp, nii]))
