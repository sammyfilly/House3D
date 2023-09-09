# Copyright 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import tqdm
import cv2
import numpy as np
import os
import queue
import time
import argparse

from House3D import objrender, Environment, load_config, House
from House3D.objrender import RenderMode
from threading import Thread, Lock

RANDOM_SEED = 0

MAX_QSIZE = 20
LOADING_THREADS = 10
RENDER_THREADS = 1

SAMPLES_PER_ROOM = 3
ROOM_TYPES = {'living_room'}

# Not sure what scale this is, it seems like it's not in meters!
ROBOT_RAD = 0.5
ROBOT_HEIGHT = 1.0

RENDER_MODES = [
    RenderMode.RGB,
    RenderMode.DEPTH,
    RenderMode.SEMANTIC,
    RenderMode.INSTANCE,
    RenderMode.INVDEPTH,
]
RENDER_NAMES = ['rgb', 'depth', 'semantic', 'instance', 'invdepth']


class RestrictedHouse(House):
    def __init__(self, **kwargs):
        super(RestrictedHouse, self).__init__(**kwargs)

    def _getRegionsOfInterest(self):
        result = []
        for roomTp in ROOM_TYPES:
            rooms = self._getRooms(roomTp)
            result.extend(self._getRoomBounds(room) for room in rooms)
        return result


def create_house(houseID, config, robotRadius=ROBOT_RAD):
    print(f'Loading house {houseID}')
    objFile = os.path.join(config['prefix'], houseID, 'house.obj')
    jsonFile = os.path.join(config['prefix'], houseID, 'house.json')
    assert os.path.isfile(objFile) and os.path.isfile(
        jsonFile
    ), f'[Environment] house objects not found! objFile=<{objFile}>'
    cachefile = os.path.join(config['prefix'], houseID, 'cachedmap1k.pkl')
    if not os.path.isfile(cachefile):
        cachefile = None

    return RestrictedHouse(
        JsonFile=jsonFile,
        ObjFile=objFile,
        MetaDataFile=config["modelCategoryFile"],
        CachedFile=cachefile,
        RobotRadius=robotRadius,
        SetTarget=False,
        ApproximateMovableMap=True,
    )


def get_house_dir(houseID):
    return os.path.join(args.output, houseID)


def gen_rand_house(cfg):
    all_house_ids = os.listdir(cfg['prefix'])
    np.random.shuffle(all_house_ids)
    for houseID in all_house_ids:
        house_dir = get_house_dir(houseID)
        if os.path.exists(house_dir):
            print(f'{house_dir} already exists, skipping')
            continue
        yield houseID


def reset_random(env, house, room):
    location = house.getRandomLocationForRoom(room)
    if not location:
        return False

    env.reset(*location)
    return True


def render_current_location(env, houseID, room_type, index):
    output_dir = get_house_dir(houseID)
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)
        print(f'Created directory {output_dir}')

    for mode_idx in range(len(RENDER_MODES)):
        render_mode = RENDER_MODES[mode_idx]
        render_name = RENDER_NAMES[mode_idx]

        env.set_render_mode(render_mode)
        img = env.render_cube_map(copy=True)
        if render_mode == RenderMode.DEPTH:
            img = img[:, :, 0]
        elif render_mode == RenderMode.INVDEPTH:
            img16 = img.astype(np.uint16)
            img = img16[:, :, 0] * 256 + img16[:, :, 1]
        else:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        output_filename = (
            f'{houseID}-room_{room_type}-loc_{index}-render_{render_name}.png'
        )
        cv2.imwrite(os.path.join(output_dir, output_filename), img)


def get_valid_rooms(house):
    result = []
    for room in house.all_rooms:
        for tp in room['roomTypes']:
            if tp.lower() in ROOM_TYPES:
                result.append(room)
                break
    return result


def house_loader(house_gen, cfg, house_queue, gen_lock):
    while True:
        while house_queue.qsize() > MAX_QSIZE:
            # Wait until we clear up the queue
            time.sleep(0)

        houseID = None
        with gen_lock:
            try:
                houseID = next(house_gen)
            except StopIteration:
                print('Done processing houses, stopping loading thread...')
                return

        house = None
        try:
            house = create_house(houseID, cfg)
        except Exception as e:
            print(f'!! Error loading house {houseID}: {e}')
            continue

        house_queue.put((houseID, house))
        print(f'Put house {houseID} in queue, total: {house_queue.qsize()}')


def house_renderer(cfg, house_queue, progress_queue):
    while True:
        houseID, house = house_queue.get()
        api = objrender.RenderAPIThread(w=args.width, h=args.height, device=0)
        env = Environment(api, house, cfg)

        loc_idx = 0
        valid_rooms = get_valid_rooms(house)
        for room in valid_rooms:
            for _i in range(SAMPLES_PER_ROOM):
                if not reset_random(env, house, room):
                    print(f'Unable to sample location for house {houseID}')
                    break
                render_current_location(env, houseID, room['id'], loc_idx)
                loc_idx += 1

        house_queue.task_done()
        progress_queue.put(1)
        print(f'Rendered house {houseID}')


def progress_tracker(total, progress_queue):
    tracker = tqdm.trange(total)
    while True:
        count = progress_queue.get()
        tracker.update(count)
        progress_queue.task_done()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', help='output directory', default='./')
    parser.add_argument('--width', type=int, default=1024)
    parser.add_argument('--height', type=int, default=1024)
    args = parser.parse_args()
    assert os.path.isdir(args.output)

    np.random.seed(RANDOM_SEED)

    cfg = load_config('config.json')
    total = len(os.listdir(cfg['prefix']))
    house_gen = gen_rand_house(cfg)
    gen_lock = Lock()
    house_queue = queue.Queue()
    progress_queue = queue.Queue()

    loader_threads = []
    for _i in range(LOADING_THREADS):
        t = Thread(
            target=house_loader, args=(house_gen, cfg, house_queue, gen_lock))
        t.start()
        loader_threads.append(t)

    render_threads = []
    for _i in range(RENDER_THREADS):
        t = Thread(
            target=house_renderer, args=(cfg, house_queue, progress_queue))
        t.daemon = True
        t.start()
        render_threads.append(t)

    progress_thread = Thread(
        target=progress_tracker, args=(total, progress_queue))
    progress_thread.daemon = True
    progress_thread.start()

    # Wait for queue to be fully populated
    for t in loader_threads:
        t.join()

    # Wait for queue to be fully processed
    house_queue.join()
    print('Done processing!')
