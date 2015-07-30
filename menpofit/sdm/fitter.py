from __future__ import division
import numpy as np
from functools import partial
import warnings
from menpo.transform import Scale
from menpo.feature import no_op
from menpofit.visualize import print_progress
from menpofit.base import batch, name_of_callable
from menpofit.builder import (scale_images, rescale_images_to_reference_shape,
                              compute_reference_shape, MenpoFitBuilderWarning,
                              compute_features)
from menpofit.fitter import (MultiFitter, noisy_shape_from_bounding_box,
                             align_shape_with_bounding_box)
from menpofit.result import MultiFitterResult
import menpofit.checks as checks
from .algorithm import Newton


# TODO: document me!
class SupervisedDescentFitter(MultiFitter):
    r"""
    """
    def __init__(self, images, group=None, bounding_box_group=None,
                 reference_shape=None, sd_algorithm_cls=Newton,
                 holistic_feature=no_op, patch_features=no_op,
                 patch_shape=(17, 17), diagonal=None, scales=(0.5, 1.0),
                 n_iterations=6, n_perturbations=30,
                 perturb_from_bounding_box=noisy_shape_from_bounding_box,
                 batch_size=None, verbose=False):
        # check parameters
        checks.check_diagonal(diagonal)
        scales, n_scales = checks.check_scales(scales)
        patch_features = checks.check_features(patch_features, n_scales)
        holistic_features = checks.check_features(holistic_feature, n_scales)
        patch_shape = checks.check_patch_shape(patch_shape, n_scales)
        # set parameters
        self.algorithms = []
        self.reference_shape = reference_shape
        self._sd_algorithm_cls = sd_algorithm_cls
        self.features = holistic_features
        self._patch_features = patch_features
        self._patch_shape = patch_shape
        self.diagonal = diagonal
        self.scales = scales
        self.n_perturbations = n_perturbations
        self.n_iterations = checks.check_max_iters(n_iterations, n_scales)
        self._perturb_from_bounding_box = perturb_from_bounding_box
        # set up algorithms
        self._setup_algorithms()

        # Now, train the model!
        self._train(images, group=group, bounding_box_group=bounding_box_group,
                    verbose=verbose, increment=False, batch_size=batch_size)

    def _setup_algorithms(self):
        for j in range(self.n_scales):
            self.algorithms.append(self._sd_algorithm_cls(
                features=self._patch_features[j],
                patch_shape=self._patch_shape[j],
                n_iterations=self.n_iterations[j]))

    def perturb_from_bounding_box(self, bounding_box):
        return self._perturb_from_bounding_box(self.reference_shape,
                                               bounding_box)

    def _train(self, images, group=None, bounding_box_group=None,
               verbose=False, increment=False, batch_size=None):

        # If batch_size is not None, then we may have a generator, else we
        # assume we have a list.
        if batch_size is not None:
            # Create a generator of fixed sized batches. Will still work even
            # on an infinite list.
            image_batches = batch(images, batch_size)
        else:
            image_batches = [list(images)]

        for k, image_batch in enumerate(image_batches):
            # After the first batch, we are incrementing the model
            if k > 0:
                increment = True

            if verbose:
                print('Computing batch {} - ({})'.format(k, len(image_batch)))

            # In the case where group is None, we need to get the only key so
            # that we can attach landmarks below and not get a complaint about
            # using None
            if group is None:
                group = image_batch[0].landmarks.group_labels[0]

            if self.reference_shape is None:
                # If no reference shape was given, use the mean of the first
                # batch
                if batch_size is not None:
                    warnings.warn('No reference shape was provided. The mean '
                                  'of the first batch will be the reference '
                                  'shape. If the batch mean is not '
                                  'representative of the true mean, this may '
                                  'cause issues.', MenpoFitBuilderWarning)
                self.reference_shape = compute_reference_shape(
                    [i.landmarks[group].lms for i in image_batch],
                    self.diagonal, verbose=verbose)

            # Rescale to existing reference shape
            image_batch = rescale_images_to_reference_shape(
                image_batch, group, self.reference_shape,
                verbose=verbose)

            # No bounding box is given, so we will use the ground truth box
            if bounding_box_group is None:
                # It's important to use bb_group for batching, so that we
                # generate ground truth bounding boxes for each batch, every
                # time
                bb_group = '__gt_bb_'
                for i in image_batch:
                    gt_s = i.landmarks[group].lms
                    perturb_bbox_group = bb_group + '0'
                    i.landmarks[perturb_bbox_group] = gt_s.bounding_box()
            else:
                bb_group = bounding_box_group

            # Find all bounding boxes on the images with the given bounding
            # box key
            all_bb_keys = list(image_batch[0].landmarks.keys_matching(
                '*{}*'.format(bb_group)))
            n_perturbations = len(all_bb_keys)

            # If there is only one example bounding box, then we will generate
            # more perturbations based on the bounding box.
            if n_perturbations == 1:
                msg = '- Generating {} new initial bounding boxes ' \
                      'per image'.format(self.n_perturbations)
                wrap = partial(print_progress, prefix=msg, verbose=verbose)

                for i in wrap(image_batch):
                    # We assume that the first bounding box is a valid
                    # perturbation thus create n_perturbations - 1 new bounding
                    # boxes
                    for j in range(1, self.n_perturbations):
                        gt_s = i.landmarks[group].lms.bounding_box()
                        bb = i.landmarks[all_bb_keys[0]].lms

                        # This is customizable by passing in the correct method
                        p_s = self._perturb_from_bounding_box(gt_s, bb)
                        perturb_bbox_group = '{}_{}'.format(bb_group, j)
                        i.landmarks[perturb_bbox_group] = p_s
            elif n_perturbations != self.n_perturbations:
                warnings.warn('The original value of n_perturbation {} '
                              'will be reset to {} in order to agree with '
                              'the provided bounding_box_group.'.
                              format(self.n_perturbations, n_perturbations),
                              MenpoFitBuilderWarning)
                self.n_perturbations = n_perturbations

            # Re-grab all the bounding box keys for iterating over when
            # calculating perturbations
            all_bb_keys = list(image_batch[0].landmarks.keys_matching(
                '*{}*'.format(bb_group)))

            # for each scale (low --> high)
            current_shapes = []
            for j in range(self.n_scales):
                if verbose:
                    if len(self.scales) > 1:
                        scale_prefix = '  - Scale {}: '.format(j)
                    else:
                        scale_prefix = '  - '
                else:
                    scale_prefix = None

                # Handle features
                if j == 0 or self.features[j] is not self.features[j - 1]:
                    # Compute features only if this is the first pass through
                    # the loop or the features at this scale are different from
                    # the features at the previous scale
                    feature_images = compute_features(image_batch,
                                                      self.features[j],
                                                      level_str=scale_prefix,
                                                      verbose=verbose)
                # handle scales
                if self.scales[j] != 1:
                    # Scale feature images only if scale is different than 1
                    scaled_images = scale_images(feature_images, self.scales[j],
                                                 level_str=scale_prefix,
                                                 verbose=verbose)
                else:
                    scaled_images = feature_images

                # Extract scaled ground truth shapes for current scale
                scaled_shapes = [i.landmarks[group].lms for i in scaled_images]

                if j == 0:
                    msg = '{}Generating {} perturbations per image'.format(
                        scale_prefix, self.n_perturbations)
                    wrap = partial(print_progress, prefix=msg,
                                   end_with_newline=False, verbose=verbose)

                    # Extract perturbations at the very bottom level
                    for i in wrap(scaled_images):
                        c_shapes = []
                        for perturb_bbox_group in all_bb_keys:
                            bbox = i.landmarks[perturb_bbox_group].lms
                            c_s = align_shape_with_bounding_box(
                                self.reference_shape, bbox)
                            c_shapes.append(c_s)
                        current_shapes.append(c_shapes)

                # train supervised descent algorithm
                if not increment:
                    current_shapes = self.algorithms[j].train(
                        scaled_images, scaled_shapes, current_shapes,
                        level_str=scale_prefix, verbose=verbose)
                else:
                    current_shapes = self.algorithms[j].increment(
                        scaled_images, scaled_shapes, current_shapes,
                        level_str=scale_prefix, verbose=verbose)

                # Scale current shapes to next resolution, don't bother
                # scaling final level
                if j != (self.n_scales - 1):
                    transform = Scale(self.scales[j + 1] / self.scales[j],
                                      n_dims=2)
                    for image_shapes in current_shapes:
                        for shape in image_shapes:
                            transform.apply_inplace(shape)

    def increment(self, images, group=None, bounding_box_group=None,
                  verbose=False, batch_size=None):
        return self._train(images, group=group,
                           bounding_box_group=bounding_box_group,
                           verbose=verbose,
                           increment=True, batch_size=batch_size)

    def _fitter_result(self, image, algorithm_results, affine_correction,
                       gt_shape=None):
        return MultiFitterResult(image, self, algorithm_results,
                                 affine_correction, gt_shape=gt_shape)

    def __str__(self):
        if self.diagonal is not None:
            diagonal = self.diagonal
        else:
            y, x = self.reference_shape.range()
            diagonal = np.sqrt(x ** 2 + y ** 2)
        is_custom_perturb_func = (self._perturb_from_bounding_box !=
                                  noisy_shape_from_bounding_box)
        regressor_cls = self.algorithms[0]._regressor_cls

        # Compute scale info strings
        scales_info = []
        lvl_str_tmplt = r"""  - Scale {}
   - {} iterations
   - Patch shape: {}"""
        for k, s in enumerate(self.scales):
            scales_info.append(lvl_str_tmplt.format(s,
                                                    self.n_iterations[k],
                                                    self._patch_shape[k]))
        scales_info = '\n'.join(scales_info)

        cls_str = r"""Supervised Descent Method
 - Regression performed using the {reg_alg} algorithm
   - Regression class: {reg_cls}
 - Scales: {scales}
{scales_info}
 - Perturbations generated per shape: {n_perturbations}
 - Images scaled to diagonal: {diagonal:.2f}
 - Custom perturbation scheme used: {is_custom_perturb_func}""".format(
            reg_alg=name_of_callable(self._sd_algorithm_cls),
            reg_cls=name_of_callable(regressor_cls),
            scales=self.scales,
            scales_info=scales_info,
            n_perturbations=self.n_perturbations,
            diagonal=diagonal,
            is_custom_perturb_func=is_custom_perturb_func)
        return cls_str


# Aliases for common combinations of supervised descent fitting
SDM = partial(SupervisedDescentFitter, sd_algorithm_cls=Newton)

class RegularizedSDM(SupervisedDescentFitter):

    def __init__(self, images, group=None, bounding_box_group=None,
                 alpha=1.0, reference_shape=None,
                 holistic_feature=no_op, patch_features=no_op,
                 patch_shape=(17, 17), diagonal=None, scales=(0.5, 1.0),
                 n_iterations=6, n_perturbations=30,
                 perturb_from_bounding_box=noisy_shape_from_bounding_box,
                 batch_size=None, verbose=False):
        super(RegularizedSDM, self).__init__(
            images, group=group,  bounding_box_group=bounding_box_group,
            reference_shape=reference_shape,
            sd_algorithm_cls=partial(Newton, alpha=alpha),
            holistic_feature=holistic_feature, patch_features=patch_features,
            patch_shape=patch_shape, diagonal=diagonal, scales=scales,
            n_iterations=n_iterations, n_perturbations=n_perturbations,
            perturb_from_bounding_box=perturb_from_bounding_box,
            batch_size=batch_size, verbose=verbose)
