# Copyright 2015 Leon Sixt
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import absolute_import
from __future__ import print_function

import math
import os
import sys

import numpy as np
import pycuda.driver as pycu
import pycuda.gpuarray as gpuarray
import theano
import theano.sandbox.cuda as thcu
import theano.tensor as T
from beras.util import smooth, sobel
from beesgrid import MASK, MASK_BLACK, MASK_WHITE
from dotmap import DotMap
from pycuda.compiler import SourceModule
import theano.misc.pycuda_init
from deepdecoder.utils import binary_mask, adaptive_mask
from deepdecoder.transform import pyramid_gaussian
import keras.objectives
_has_cuda = True
try:
    from theano.misc.pycuda_utils import to_gpuarray, to_cudandarray
    from theano.sandbox.cuda import CudaNdarrayType
except ImportError:
    _has_cuda = False


def mask_split_kernel_code():
    mask_split_file = os.path.join(os.path.dirname(__file__),
                                   'cuda/mask_split.cu')
    with open(mask_split_file) as f:
        return f.read()


def shape_ok(shp):
    return shp[2] == shp[3] and shp[2] % 2 == 0 and shp[1] == 1


def contiguouse(x):
    return thcu.basic_ops.gpu_contiguous(
        thcu.basic_ops.as_cuda_ndarray_variable(x))


def pycuda_zeros(arr, shape):
    if arr is None or arr.shape != shape:
        arr = gpuarray.zeros(shape, dtype=np.float32)
    else:
        if type(arr) != gpuarray.GPUArray:
            arr = to_gpuarray(arr)
    pycu.memset_d32(arr.gpudata, 0, arr.size)
    return arr


class SplitMaskGrad(theano.Op):
    __props__ = ()

    def __init__(self, connected=None):
        super().__init__()
        if connected is None:
            self.connected = ["sum", "pow"]
        else:
            self.connected = connected
        if "sum" in self.connected and "pow" in self.connected:
            self.function_name = "image_mask_split_grad_sum_pow"
        elif "sum" in self.connected:
            self.function_name = "image_mask_split_grad_sum"
        elif "pow" in self.connected:
            self.function_name = "image_mask_split_grad_pow"
        else:
            raise ValueError("At least sum or pow gradient must be provided")

    def make_node(self, mask_idx, image, og_sum, og_pow):
        mask_idx = contiguouse(mask_idx)
        image = contiguouse(image)
        inputs = [mask_idx, image]
        if str(og_sum) == "<DisconnectedType>" and \
                str(og_pow) == "<DisconnectedType>":
            raise ValueError("At least sum or pow gradient must be provided")

        if str(og_sum) != "<DisconnectedType>":
            og_sum = contiguouse(og_sum)
            inputs.append(og_sum)
        if str(og_pow) != "<DisconnectedType>":
            og_pow = contiguouse(og_pow)
            inputs.append(og_pow)

        output_type = CudaNdarrayType(broadcastable=(False,)*4)
        return theano.Apply(self, inputs,
                            [output_type()])

    def make_thunk(self, node, storage_map, _, _2):
        inputs = [storage_map[v] for v in node.inputs]
        outputs = [storage_map[v] for v in node.outputs]
        mod = SourceModule(mask_split_kernel_code(), no_extern_c=1)
        image_mask_split_grad = mod.get_function(self.function_name)

        def thunk():
            grad = outputs[0][0]
            mask_idx = inputs[0][0]
            assert shape_ok(mask_idx.shape)
            s = mask_idx.shape[3]
            block_dim = min(32, s)
            grid_dim = math.ceil(s / block_dim)
            mask_idx = to_gpuarray(mask_idx, copyif=True)

            image = inputs[1][0]
            assert shape_ok(image.shape)
            image = to_gpuarray(image, copyif=True)

            batch_size = min(mask_idx.shape[0], image.shape[0])
            grad_shape = (batch_size, 1, s, s)
            grad = pycuda_zeros(grad, grad_shape)
            grid = (batch_size, grid_dim, grid_dim)
            block = (1, block_dim, block_dim)
            if "sum" in self.connected and "pow" in self.connected:
                og_sum = to_gpuarray(inputs[2][0], copyif=True)
                og_pow = to_gpuarray(inputs[3][0], copyif=True)
                image_mask_split_grad(
                    mask_idx, image, og_sum, og_pow,
                    np.int32(batch_size), np.int32(s), grad,
                    block=block, grid=grid)
            elif "sum" in self.connected:
                og_sum = to_gpuarray(inputs[2][0], copyif=True)
                image_mask_split_grad(
                    mask_idx, image, og_sum,
                    np.int32(batch_size), np.int32(s), grad,
                    block=block, grid=grid)
            elif "pow" in self.connected:
                og_pow = to_gpuarray(inputs[2][0], copyif=True)
                image_mask_split_grad(
                    mask_idx, image, og_pow,
                    np.int32(batch_size), np.int32(s), grad,
                    block=block, grid=grid)
            outputs[0][0] = to_cudandarray(grad)

        return thunk


class SplitMask(theano.Op):
    __props__ = ()

    def make_node(self, mask_idx, image):
        mask_idx = contiguouse(mask_idx)
        image = contiguouse(image)
        assert mask_idx.dtype == "float32"
        assert image.dtype == "float32"
        output_type = CudaNdarrayType(broadcastable=(False,)*5)
        return theano.Apply(self, [mask_idx, image],
                            [output_type(), output_type(), output_type()])

    def make_thunk(self, node, storage_map, _, _2):
        inputs = [storage_map[v] for v in node.inputs]
        outputs = [storage_map[v] for v in node.outputs]
        mod = SourceModule(mask_split_kernel_code(), no_extern_c=1)
        image_mask_split = mod.get_function("image_mask_split")
        self._sdata = None

        def thunk():
            mask_idx = inputs[0][0]
            image = inputs[1][0]
            batch_size = min(mask_idx.shape[0], image.shape[0])
            assert shape_ok(mask_idx.shape)
            assert shape_ok(image.shape)
            mask_idx = to_gpuarray(mask_idx)
            image = to_gpuarray(image)
            s = mask_idx.shape[3]
            assert mask_idx.shape[2] == mask_idx.shape[3], \
                "height and width must be equal"

            sdata_shape = (3*len(MASK), batch_size, 1, s, s)
            self._sdata = pycuda_zeros(self._sdata, sdata_shape)
            blocks_max = 32
            blocks_s = min(blocks_max, s)
            grid_s = math.ceil(s / blocks_max)
            grid = (batch_size, grid_s, grid_s)
            block = (1, blocks_s, blocks_s)
            image_mask_split(mask_idx, image, np.int32(batch_size),
                             np.int32(s), self._sdata,
                             block=block, grid=grid)
            sdata_as_theano = to_cudandarray(self._sdata)
            m = len(MASK)
            outputs[0][0] = sdata_as_theano[:m]
            outputs[1][0] = sdata_as_theano[m:2*m]
            outputs[2][0] = sdata_as_theano[2*m:]

        return thunk

    def grad(self, inputs, output_gradients):
        grad_ins = inputs + output_gradients[:2]
        connected = []
        if str(output_gradients[0]) != "<DisconnectedType>":
            connected.append("sum")
        if str(output_gradients[1]) != "<DisconnectedType>":
            connected.append("pow")
        return [T.zeros_like(inputs[0]), SplitMaskGrad(connected)(*grad_ins)]


cuda_split_mask = SplitMask()


def theano_split_mask(mask_idx, image):
    shp = image.shape
    count = T.zeros((len(MASK), shp[0]))
    splitted_list = []
    for i, (name, enum_val) in enumerate(MASK.items()):
        idx = T.eq(mask_idx, enum_val)
        count = T.set_subtensor(count[i], idx.sum(axis=(1, 2, 3)))
        tmp_image = T.zeros_like(image)
        tmp_image = T.set_subtensor(tmp_image[idx.nonzero()],
                                    image[idx.nonzero()])
        splitted_list.append(tmp_image)
    splitted = T.stack(splitted_list)
    return splitted, splitted**2, \
        count.reshape((len(MASK), shp[0], 1, 1, 1), ndim=5)


def mean_by_count(x, count, axis=0):
    return T.switch(T.eq(count, 0),
                    T.zeros_like(count),
                    x.sum(axis) / count)


def split_to_mean_var(sum, pow, count):
    axis = [2, 3, 4]
    count_sum = count.sum(axis)
    mean = mean_by_count(sum, count_sum, axis)
    var = mean_by_count(pow, count_sum, axis) - mean**2
    return mean, var, count_sum


def get_split_mask_fn(impl='auto'):
    if (impl == 'auto' and theano.config.device.startswith("gpu")) \
            or impl == "cuda":
        return SplitMask()
    else:
        if theano.config.device.startswith("gpu"):
            print("Warning: Possible very slow. GPU is avialable but still "
                  "computing mask_loss on the CPU", file=sys.stderr)
        return theano_split_mask


def median(grid_split_image, grid_counts):
    shp = grid_split_image.shape
    reshape = grid_split_image.reshape((shp[0], shp[1], shp[2], -1), ndim=4)
    img_sort = T.sort(reshape)
    pixels = img_sort.shape[-1]
    indicies = (pixels + grid_counts) // 2
    indicies = T.cast(indicies, 'int32')
    medians = img_sort.take(indicies)
    return medians


def segment_means(grid_idx, image, impl='auto'):
    split_fn = get_split_mask_fn(impl)
    mean, var, count = split_to_mean_var(*split_fn(grid_idx, image))

    def slice_mean(slice):
        return (mean[slice]*count[slice]).sum(axis=0) / \
            count[slice].sum(axis=0)

    mask_keys = list(MASK.keys())
    ignore_idx = mask_keys.index("IGNORE")
    black_mean = slice_mean(slice(0, ignore_idx))
    white_mean = slice_mean(slice(ignore_idx+1, None))
    ignore_mean = slice_mean(slice(ignore_idx, ignore_idx+2))
    return black_mean, white_mean, ignore_mean


def mask_loss_adaptive_mse(grid_idx, image, impl='auto'):
    black_mean, white_mean, _ = segment_means(grid_idx, image, impl)
    white_mean = T.maximum(white_mean, 0.40)
    white_mean = T.maximum(white_mean, black_mean + 0.20)
    black_mean = T.minimum(white_mean - 0.20, black_mean)
    dimsuffle = (0, 'x', 'x', 'x')
    bw = adaptive_mask(grid_idx, ignore=0.0,
                       black=black_mean.dimshuffle(*dimsuffle),
                       white=white_mean.dimshuffle(*dimsuffle))
    # bw = smooth(bw, sigma=2.)
    diff = T.zeros_like(bw)
    idx = T.bitwise_and(T.neq(grid_idx, MASK["IGNORE"]),
                        T.neq(grid_idx, MASK["BACKGROUND_RING"]))
    diff = T.set_subtensor(diff[idx.nonzero()], abs(bw - image)[idx.nonzero()])
    loss = (T.maximum(diff, 0.15)[idx.nonzero()]**2).mean()
    return DotMap({
        'loss': loss,
        'visual': {
            'diff': diff,
            'bw_grid': bw
        }
    })


def mask_loss_mse(grid_idx, image):
    indicies = T.bitwise_and(T.neq(grid_idx, MASK["IGNORE"]),
                             T.neq(grid_idx, MASK["BACKGROUND_RING"]))
    bw = binary_mask(grid_idx, ignore=0.0)
    diff = (bw - image)
    loss = (diff[indicies.nonzero()]**2).mean()
    visual_diff = T.zeros_like(diff)
    visual_diff = T.set_subtensor(visual_diff[indicies.nonzero()],
                                  diff[indicies.nonzero()]**2)
    return DotMap({
        'loss': loss,
        'visual': {
            'diff': visual_diff,
            'bw_grid': bw
        }
    })


def mask_loss_sobel(grid_idx, image, impl='auto', diff_type='mse', scale=10.):
    def norm_by_max(x):
        maxs = T.max(x, axis=[1, 2, 3])
        return x / maxs.dimshuffle(0, 'x', 'x', 'x')

    def clip_around_zero(x, threshold=0.2):
        indicies = T.bitwise_and(x < threshold, x > -threshold)
        return T.set_subtensor(x[indicies.nonzero()], 0)

    bw = binary_mask(grid_idx, ignore=0.5)
    bw = smooth(bw, sigma=2.)
    sobel_bw = sobel(bw)
    sobel_img = [smooth(s, sigma=1.) for s in sobel(image)]
    sobel_bw = [norm_by_max(s) for s in sobel_bw]
    sobel_img = [norm_by_max(s) for s in sobel_img]

    if diff_type == 'correlation':
        loss_x = -sobel_bw[0] * sobel_img[0]
        loss_y = -sobel_bw[1] * sobel_img[1]
        loss = T.mean(loss_x + loss_y) / 2
    elif diff_type == 'mse':
        sobel_bw = [clip_around_zero(s) for s in sobel_bw]
        sobel_img = [clip_around_zero(s) for s in sobel_img]
        loss_x = (sobel_bw[0] - sobel_img[0])**2
        loss_y = (sobel_bw[1] - sobel_img[1])**2
        loss = T.mean(loss_x + loss_y) / 2
    else:
        raise ValueError("unknown diff_type: {}".format(diff_type))

    return DotMap({
        'loss': scale*T.mean(loss),
        'visual': {
            'loss_x': loss_x,
            'loss_y': loss_y,
            'sobel_img_x': sobel_img[0],
            'sobel_img_y': sobel_img[1],
            'sobel_mask_x': sobel_bw[0],
            'sobel_mask_y': sobel_bw[1],
        },
        'loss_x_mean': loss_x.mean(),
        'loss_y_mean': loss_x.mean(),
    })


def mask_loss(mask_image, image, impl='auto', scale=50, mean_weight=1.,
              var_weight=4,):
    split_fn = get_split_mask_fn(impl)
    mean, var, count = split_to_mean_var(*split_fn(mask_image, image))

    def slice_mean(slice):
        return (mean[slice]*count[slice]).sum(axis=0) / \
            count[slice].sum(axis=0)

    mask_keys = list(MASK.keys())
    ignore_idx = mask_keys.index("IGNORE")
    black_mean = slice_mean(slice(0, ignore_idx))
    white_mean = slice_mean(slice(ignore_idx+1, None))
    min_distance = 0.25 * T.ones_like(black_mean)
    mean_distance = T.minimum(white_mean - black_mean, min_distance)
    black_white_loss = (mean_distance - min_distance)**2
    background_ring_idx = mask_keys.index("BACKGROUND_RING")
    outer_white_ring_idx = mask_keys.index("OUTER_WHITE_RING")
    ring_min_distance = 0.1 * T.ones_like(black_mean)
    ring_distance = T.minimum(mean[outer_white_ring_idx] -
                              mean[background_ring_idx],
                              ring_min_distance)
    ring_loss = (ring_distance - ring_min_distance)**2 + \
        0.25*var[background_ring_idx]
    cell_losses = []

    def cell_loss_fn(mask_color, color_mean, mean_weight=1., var_weight=4.):
        cell_idx = mask_keys.index(mask_color)
        cell_mean = mean[cell_idx]
        cell_weight = count[cell_idx].sum()
        mean_tolerance = 0.10**2
        mean_diff = T.maximum((color_mean - cell_mean)**2, mean_tolerance) - \
            mean_tolerance
        return T.switch(T.eq(count[cell_idx], 0),
                        T.zeros_like(black_mean),
                        cell_weight * (
                            mean_weight*mean_diff + var_weight*var[cell_idx]
                        ))
    for black_parts in MASK_BLACK:
        cell_losses.append(cell_loss_fn(black_parts, black_mean,
                                        mean_weight, var_weight))
    for white_parts in MASK_WHITE:
        if white_parts == ["OUTER_WHITE_RING"]:
            cell_losses.append(cell_loss_fn(white_parts, white_mean,
                                            mean_weight/10, var_weight/40))
        else:
            cell_losses.append(cell_loss_fn(white_parts, white_mean,
                                            mean_weight, var_weight))

    cell_losses = [l / (count[:background_ring_idx].sum() +
                        count[ignore_idx+1:].sum())
                   for l in cell_losses]

    cell_loss = sum(cell_losses)
    loss = black_white_loss + ring_loss + cell_loss
    return DotMap({
        'loss': scale*T.mean(loss),
        'loss_per_sample': scale*loss,
        'black_white_loss': scale*black_white_loss,
        'ring_loss': scale*ring_loss,
        'cell_losses': scale*cell_losses,
    })


def pyramid_loss(grid_idx, image):
    def mean(mask):
        return T.sum(image*mask, axis=(1, 2, 3)) / T.sum(mask, axis=(1, 2, 3))

    min_mean_distance = 0.2
    max_grayness = 0.3
    # use 16x16 as last resolution
    max_layer = 3

    black_mask = binary_mask(grid_idx, ignore=0, black=1, white=0)
    white_mask = binary_mask(grid_idx, ignore=0, black=0, white=1)

    black_mean = mean(black_mask)
    white_mean = mean(white_mask)

    mean_diff = white_mean - black_mean
    gray_mean = (white_mean + black_mean) / 2
    black_mean = T.where(mean_diff < min_mean_distance,
                         gray_mean - min_mean_distance/2,
                         black_mean)
    white_mean = T.where(mean_diff < min_mean_distance,
                         gray_mean + min_mean_distance/2,
                         white_mean)

    dimshuffle = (0, 'x', 'x', 'x')
    white_mean = white_mean.dimshuffle(*dimshuffle)
    black_mean = black_mean.dimshuffle(*dimshuffle)

    mean_half_dist = (white_mean - black_mean) / 2

    gauss_pyr_black = list(pyramid_gaussian(black_mask, max_layer))
    gauss_pyr_white = list(pyramid_gaussian(white_mask, max_layer))
    gauss_pyr_image = list(pyramid_gaussian(image, max_layer))

    white_diff = white_mean*gauss_pyr_white[-1] - \
        gauss_pyr_image[-1]*gauss_pyr_white[-1]
    black_diff = gauss_pyr_image[-1]*gauss_pyr_black[-1] - \
        black_mean*gauss_pyr_black[-1]

    white_diff_thres = T.maximum(white_diff - max_grayness*(mean_half_dist), 0)
    black_diff_thres = T.maximum(black_diff - max_grayness*(mean_half_dist), 0)

    loss_white = white_diff_thres.sum(axis=(1, 2, 3)) / \
        gauss_pyr_white[-1].sum(axis=(1, 2, 3))
    loss_black = black_diff_thres.sum(axis=(1, 2, 3)) / \
        gauss_pyr_black[-1].sum(axis=(1, 2, 3))
    loss = (loss_white + loss_black).mean()
    return DotMap({
        'loss': loss,
        'visual': {
            'gauss_pyr_image': gauss_pyr_image[-1],
            'gauss_pyr_black': gauss_pyr_black[-1],
            'black_diff': black_diff,
            'black_diff_thres': black_diff_thres,
            'gauss_pyr_white': gauss_pyr_white[-1],
            'white_diff': white_diff,
            'white_diff_thres': white_diff_thres,
        },
        'print': {
            '0 black_mean': black_mean,
            '1 mean_half_dist': mean_half_dist,
            '2 white_mean': white_mean,
            '3 loss_black': loss_black,
            '4 loss_white': loss_white,
            '5 loss': loss_black + loss_white,
        }
    })


def pyramid_mse_image_loss(real_tag, reconstructed):
    max_layer = 2

    tag = reconstructed
    tag_idx = (tag >= 0).nonzero()
    selection = T.zeros_like(tag)
    selection = T.set_subtensor(selection[tag_idx], 1)

    gauss_pyr_real_tag = list(pyramid_gaussian(real_tag, max_layer))
    gauss_pyr_real_tag = list(pyramid_gaussian(real_tag, max_layer))

    real_tag_down = smooth(gauss_pyr_real_tag[-1])
    diff = selection*real_tag_down - selection*tag

    selection_sum = T.sum(selection, axis=(1, 2, 3))
    tag_elem = real_tag_down.shape[-2] * real_tag_down.shape[-1]

    min_tag_size = tag_elem / 5
    to_small_tag = T.switch(selection_sum <= min_tag_size,
                            (min_tag_size - selection_sum)**2,
                            0.)
    squard_error = (diff**2).sum(axis=(1, 2, 3))
    loss = squard_error / selection_sum
    loss += to_small_tag
    return DotMap({
        'loss': loss.reshape((-1, 1)),
        'visual': {
            'gauss_pyr_real_tag': gauss_pyr_real_tag[-1],
            'selection': selection,
            'tag': tag,
            'diff': diff,
        },
        'print': {
            '0 loss': loss,
            '1 selection_sum': selection_sum
        }
    })


def to_keras_loss(loss_fn):
    def wrapper(y_true, y_pred):
        return loss_fn(y_true, y_pred).loss

    return wrapper
