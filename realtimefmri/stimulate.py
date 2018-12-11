#!/usr/bin/env python3
import os
import pickle
import shlex
import subprocess
import numpy as np
import redis
from realtimefmri import config
from realtimefmri.utils import get_logger


logger = get_logger('stimulate', to_console=True, to_network=True)


class Stimulus(object):
    def __init__(self, **kwargs):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def run(self):
        raise NotImplementedError


class Debug(Stimulus):
    def __init__(self, **kwargs):
        super(Debug, self).__init__()

    def run(self, inp):
        data = np.fromstring(inp['data'], dtype='float32')
        return '{}'.format(len(data))


class SendToDashboard(Stimulus):
    """Send data to the dashboard

    Parameters
    ----------
    name : str
    plot_type : str
        Type of plot

    Attributes
    ----------
    redis : redis connection
    key_name : str
        Name of the key in the redis database
    """
    def __init__(self, name, plot_type='marker', host=config.REDIS_HOST, port=6379, **kwargs):
        super(SendToDashboard, self).__init__()
        r = redis.StrictRedis(host=host, port=port)
        key_name = 'dashboard:' + name
        r.set(key_name + ':type', plot_type)

        self.redis = r
        self.key_name = key_name

    def run(self, data):
        self.redis.set(self.key_name, pickle.dumps(data))
        self.redis.set(self.key_name + ':update', b'true')


class SendToPycortexViewer(Stimulus):
    """Send data to the pycortex webgl viewer

    Parameters
    ----------
    name : str

    Attributes
    ----------
    redis : redis connection
    """
    def __init__(self, name, host=config.REDIS_HOST, port=6379, **kwargs):
        super(SendToPycortexViewer, self).__init__()
        self.redis = redis.StrictRedis(host=host, port=port)

    def run(self, data):
        self.redis.publish("viewer", pickle.dumps(data))


class RoiBars(Stimulus):
    def __init__(self, **kwargs):
        super(RoiBars, self).__init__()
        raise NotImplementedError


class AudioRecorder(object):
    """Record the microphone and save to file

    Record from the microphone and save as a ``.wav`` file inside of the
    recording folder

    Parameters
    ----------
    jack_port : str
        Name of the jack port
    file_name : str
        Relative path to file name. Will be saved inside the recording folder
    recording_id : str
        Identifier for the recording. Used as the name of the recording folder

    Attributes
    ----------
    rec_path : str
        Path where recording is saves

    Methods
    -------
    start()
        Start the recording
    stop()
        Stop the recording
    """
    def __init__(self, jack_port, file_name, recording_id, **kwargs):
        super(AudioRecorder, self).__init__()
        rec_path = os.path.join(config.RECORDING_DIR, recording_id, file_name + '.wav')
        if not os.path.exists(os.path.dirname(rec_path)):
            os.makedirs(os.path.dirname(rec_path))

        cmd = 'jack_rec -f {} -d {} {}'.format(rec_path, -1, jack_port)

        self.cmd = shlex.split(cmd)
        self.rec_path = rec_path
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(self.cmd)

    def stop(self):
        self.proc.terminate()
        inpath = self.rec_path
        outpath = self.rec_path.replace('.wav', '.mp3')
        cmd = shlex.split('lame {} {}'.format(inpath, outpath))
        with open(os.devnull, 'w') as devnull:
            subprocess.call(cmd, stdout=devnull, stderr=devnull)
        os.remove(self.rec_path)
