import logging
import numpy as np
import collections
from scipy.ndimage.morphology import distance_transform_edt, binary_erosion
from scipy.ndimage import generate_binary_structure
from gunpowder.array import Array
from gunpowder.nodes.batch_filter import BatchFilter

logger = logging.getLogger(__name__)


class AddDistance(BatchFilter):
    '''Compute array with signed distances from specific labels

    Args:

        label_array_key(:class:``ArrayKey``): The :class:``ArrayKey`` to read the labels from.

        distance_array_key(:class:``ArrayKey``): The :class:``ArrayKey`` to generate containing the values of the
            distance transform.

        mask_array_key(:class:``ArrayKey``): The :class:``ArrayKey`` to update in order to compensate for windowing
        artifacts after distance transformation.

        add_constant(scalar, optional): constant value to add to distance transform (before adapting mask for

        label_id (int, tuple, optional): ids from which to compute distance transform (defaults to 1)

        factor (int, tuple, optional): distances are downsampled by this factor
    '''

    def __init__(
            self,
            label_array_key,
            distance_array_key,
            mask_array_key,
            add_constant=None,
            label_id=None,
            factor=1):

        self.label_array_key = label_array_key
        self.distance_array_key = distance_array_key
        self.mask_array_key = mask_array_key
        if not isinstance(label_id, collections.Iterable) and label_id is not None:
            label_id = (label_id,)
        self.label_id = label_id
        self.factor = factor
        self.add_constant = add_constant

    def setup(self):

        assert self.label_array_key in self.spec, (
            "Upstream does not provide %s needed by "
            "AddDistance"%self.label_array_key)

        assert self.mask_array_key in self.spec, (
            "Upstream does not provide %s needed by "
            "AddDistance"%self.mask_array_key)

        spec = self.spec[self.label_array_key].copy()
        spec.dtype = np.float32
        spec.voxel_size *= self.factor
        self.provides(self.distance_array_key, spec)

    def prepare(self, request):

        if self.distance_array_key in request:
            del request[self.distance_array_key]

    def process(self, batch, request):

        if self.distance_array_key not in request:
            return

        voxel_size = self.spec[self.label_array_key].voxel_size
        data = batch.arrays[self.label_array_key].data
        mask = batch.arrays[self.mask_array_key].data

        if self.label_id is not None:
            binary_label = np.in1d(data.ravel(), self.label_id).reshape(data.shape)
        else:
            binary_label = data > 0

        dims = binary_label.ndim

        # check if inside a label, or if there is no label
        if binary_label.std() == 0:
            max_distance = min(dim * vs for dim, vs in zip(binary_label.shape, voxel_size))
            distances = np.ones(binary_label.shape, dtype=np.float32) * max_distance

            # no label
            if binary_label.sum() == 0:
                distances *= -1

        else:
            sampling = tuple(float(v) for v in voxel_size)
            distances = self.__signed_distance(binary_label, sampling=sampling)

        if isinstance(self.factor, tuple):
            slices = tuple(slice(None, None, k) for k in self.factor)
        else:
            slices = tuple(slice(None, None, self.factor) for _ in range(dims))

        distances = distances[slices]

        if self.add_constant is not None:
            distances += self.add_constant

        # modify in-place the label mask
        mask_voxel_size = tuple(float(v) for v in self.spec[self.mask_array_key].voxel_size)
        mask = self.__constrain_distances(mask, distances, mask_voxel_size)

        spec = self.spec[self.distance_array_key].copy()
        spec.roi = request[self.distance_array_key].roi

        batch.arrays[self.mask_array_key] = Array(mask, spec)
        batch.arrays[self.distance_array_key] = Array(distances, spec)

    @staticmethod
    def __signed_distance(label, **kwargs):
        # calculate signed distance transform relative to a binary label. Positive distance inside the object,
        # negative distance outside the object. This function estimates signed distance by taking the difference
        # between the distance transform of the label ("inner distances") and the distance transform of
        # the complement of the label ("outer distances"). To compensate for an edge effect, .5 (half a pixel's
        # distance) is added to the positive distances and subtracted from the negative distances.
        inner_distance = distance_transform_edt(binary_erosion(label, border_value=1,
                                                               structure=generate_binary_structure(label.ndim,
                                                                                                   label.ndim)),
                                                               **kwargs)
        outer_distance = distance_transform_edt(np.logical_not(label), **kwargs)
        result = inner_distance - outer_distance

        return result

    @staticmethod
    def __constrain_distances(mask, distances, mask_sampling):
        # remove elements from the mask where the label distances exceed the distance from the boundary

        tmp = np.zeros(np.array(mask.shape) + np.array((2,)*mask.ndim), dtype=mask.dtype)
        slices = tmp.ndim * (slice(1, -1),)
        tmp[slices] = mask
        boundary_distance = distance_transform_edt(binary_erosion(tmp, border_value=1,
                                                                  structure=generate_binary_structure(tmp.ndim,
                                                                                                      tmp.ndim)),
                                                                  sampling=mask_sampling)
        boundary_distance = boundary_distance[slices]
        mask_output = mask.copy()
        mask_output[abs(distances) > boundary_distance] = 0

        return mask_output
