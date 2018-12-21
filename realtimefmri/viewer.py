import pickle
import time

import numpy as np
import redis

import cortex
from realtimefmri import config
from realtimefmri.utils import get_logger

logger = get_logger('viewer', to_console=True, to_network=True)

r = redis.StrictRedis(config.REDIS_HOST)


class PyCortexViewer():
    bufferlen = 15

    def __init__(self, surface, transform, mask_type='thick', vmin=-2., vmax=2.):
        if mask_type == '':
            data = np.zeros((self.bufferlen, 30, 100, 100), 'float32')
        else:
            npts = cortex.db.get_mask(surface, transform, mask_type).sum()
            data = np.zeros((self.bufferlen, npts), 'float32')

        vol = cortex.Volume(data, surface, transform, vmin=vmin, vmax=vmax)
        logger.info('Starting pycortex viewer')
        view = cortex.webgl.show(vol, open_browser=True, autoclose=False, port=8051)
        logger.info('Started pycortex viewer %s %s %s', surface, transform, mask_type)

        self.surface = surface
        self.transform = transform
        self.mask_type = mask_type

        self.view = view
        self.active = True
        self.i = 0

    def update_viewer(self, volume):
        logger.info(f"Updating pycortex viewer")
        volume = volume.astype('float32')
        volume = cortex.Volume(volume, self.surface, self.transform)
        mosaic, _ = cortex.mosaic(volume.volume[0], show=False)

        i, = self.view.setFrame()
        i = round(i)
        new_frame = (i + 1) % self.bufferlen
        self.view.dataviews.data.data[0]._setData(new_frame, mosaic)

        i, = self.view.setFrame()
        i = round(i)
        self.view.playpause('play')
        time.sleep(1)
        self.view.playpause('pause')
        self.view.setFrame(i + 0.99)

    def run(self):
        subscriber = r.pubsub()
        subscriber.subscribe('viewer')
        logger.info('Listening for volumes')
        for message in subscriber.listen():
            if message['type'] == 'message':
                logger.info('Received volume')
                vol = pickle.loads(message['data'])
                self.update_viewer(vol)


def serve(surface, transform, mask_type, vmin=-2, vmax=2):
    viewer = PyCortexViewer(surface, transform, mask_type, vmin, vmax)
    viewer.run()
