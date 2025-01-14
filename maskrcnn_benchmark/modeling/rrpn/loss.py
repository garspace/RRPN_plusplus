# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
"""
This file contains specific functions for computing losses on the RPN
file
"""

import torch
from torch.nn import functional as F
import numpy as np
from ..balanced_positive_negative_sampler import BalancedPositiveNegativeSampler
from ..utils import cat

from maskrcnn_benchmark.layers import smooth_l1_loss
from maskrcnn_benchmark.modeling.matcher import Matcher
from maskrcnn_benchmark.structures.rboxlist_ops import boxlist_iou
from maskrcnn_benchmark.structures.rboxlist_ops import cat_boxlist

class RPNLossComputation(object):
    """
    This class computes the RPN loss.
    """

    def __init__(self, proposal_matcher, fg_bg_sampler, box_coder, edge_punished=False, OHEM=False):
        """
        Arguments:
            proposal_matcher (Matcher)
            fg_bg_sampler (BalancedPositiveNegativeSampler)
            box_coder (BoxCoder)
        """
        # self.target_preparator = target_preparator
        self.proposal_matcher = proposal_matcher
        self.fg_bg_sampler = fg_bg_sampler
        self.box_coder = box_coder
        self.edge_punished = edge_punished
        self.OHEM = OHEM

    def match_targets_to_anchors(self, anchor, target):
        match_quality_matrix = boxlist_iou(target, anchor)
        matched_idxs = self.proposal_matcher(match_quality_matrix)
        # RPN doesn't need any fields from target
        # for creating the labels, so clear them all
        target = target.copy_with_fields([])
        # get the targets corresponding GT for each anchor
        # NB: need to clamp the indices because we can have a single
        # GT in the image, and matched_idxs can be -2, which goes
        # out of bounds

        matched_targets = target[matched_idxs.clamp(min=0)]
        matched_targets.add_field("matched_idxs", matched_idxs)
        return matched_targets

    def prepare_targets(self, anchors, targets):
        labels = []
        regression_targets = []
        for anchors_per_image, targets_per_image in zip(anchors, targets):
            matched_targets = self.match_targets_to_anchors(
                anchors_per_image, targets_per_image
            )

            matched_idxs = matched_targets.get_field("matched_idxs")
            labels_per_image = matched_idxs >= 0
            labels_per_image = labels_per_image.to(dtype=torch.float32)
            # discard anchors that go out of the boundaries of the image
            #################this line leads to no positive class##################
            #labels_per_image[~anchors_per_image.get_field("visibility")] = -1
            #######################################################################

            # discard indices that are between thresholds
            inds_to_discard = matched_idxs == Matcher.BETWEEN_THRESHOLDS
            labels_per_image[inds_to_discard] = -1

            # compute regression targets
            regression_targets_per_image = self.box_coder.encode(
                matched_targets.bbox, anchors_per_image.bbox
            )

            # print('regression_targets_per_image:', regression_targets_per_image)
            # label_np = labels_per_image.data.cpu().numpy()
            # print('rpn_labels:', labels_per_image.size(), np.unique(label_np), len(np.where(label_np==1)[0]), len(np.where(label_np==0)[0]))
            labels.append(labels_per_image)
            regression_targets.append(regression_targets_per_image)

        return labels, regression_targets

    def __call__(self, anchors, objectness, box_regression, targets):
        """
        Arguments:
            anchors (list[BoxList])
            objectness (list[Tensor])
            box_regression (list[Tensor])
            targets (list[BoxList])

        Returns:
            objectness_loss (Tensor)
            box_loss (Tensor
        """
        anchors = [cat_boxlist(anchors_per_image) for anchors_per_image in anchors]
        labels, regression_targets = self.prepare_targets(anchors, targets)
        sampled_pos_inds, sampled_neg_inds = self.fg_bg_sampler(labels)
        sampled_pos_inds = torch.nonzero(torch.cat(sampled_pos_inds, dim=0)).squeeze(1)
        sampled_neg_inds = torch.nonzero(torch.cat(sampled_neg_inds, dim=0)).squeeze(1)

        # print("pos and neg:", sampled_pos_inds.shape, sampled_neg_inds.shape)

        sampled_inds = torch.cat([sampled_pos_inds, sampled_neg_inds], dim=0)

        objectness_flattened = []
        box_regression_flattened = []
        # for each feature level, permute the outputs to make them be in the
        # same format as the labels. Note that the labels are computed for
        # all feature levels concatenated, so we keep the same representation
        # for the objectness and the box_regression
        for objectness_per_level, box_regression_per_level in zip(
            objectness, box_regression
        ):
            
            N, A, H, W = objectness_per_level.shape
            objectness_per_level = objectness_per_level.permute(0, 2, 3, 1).reshape(
                N, -1
            )
            box_regression_per_level = box_regression_per_level.view(N, -1, 5, H, W)
            box_regression_per_level = box_regression_per_level.permute(0, 3, 4, 1, 2)
            box_regression_per_level = box_regression_per_level.reshape(N, -1, 5)

            objectness_flattened.append(objectness_per_level)
            box_regression_flattened.append(box_regression_per_level)
        # concatenate on the first dimension (representing the feature levels), to
        # take into account the way the labels were generated (with all feature maps
        # being concatenated as well)
        objectness = cat(objectness_flattened, dim=1).reshape(-1)
        box_regression = cat(box_regression_flattened, dim=1).reshape(-1, 5)

        labels = torch.cat(labels, dim=0)
        regression_targets = torch.cat(regression_targets, dim=0)
        box_regression = box_regression

        box_regression_pos = box_regression[sampled_pos_inds]
        regression_targets_pos = regression_targets[sampled_pos_inds]
        if self.edge_punished:
            anchors_cat = torch.cat([anchor.bbox for anchor in anchors], 0)
            pos_anchors_w = anchors_cat[:, 2:3][sampled_pos_inds]
            pos_anchors_w_norm = pos_anchors_w / (torch.mean(pos_anchors_w) + 1e-10)
            # print('box_regression_pos:', pos_anchors_w_norm.size(), box_regression_pos.size())
            box_regression_pos = pos_anchors_w_norm * box_regression_pos
            regression_targets_pos = pos_anchors_w_norm * regression_targets_pos

        plabels = labels[sampled_inds]

        if self.OHEM:
            cls_logits = objectness[sampled_inds]
            score_sig = torch.sigmoid(cls_logits)

            # pick hard positive which takes 1/4
            pos_score_sig = score_sig[plabels == 1]
            pos_num = pos_score_sig.shape[0]
            hard_pos_num  = int(pos_num / 4) + 1
            hp_vals, hp_indices = torch.topk(-pos_score_sig, hard_pos_num, dim=0)
            # hard_pos_sig = pos_score_sig[hp_indices]

            pos_label = plabels[plabels == 1]
            pos_label = pos_label[hp_indices]
            pos_logits = cls_logits[plabels == 1]
            pos_logits = pos_logits[hp_indices]

            pos_box_reg = box_regression_pos[hp_indices]
            pos_box_target = regression_targets_pos[hp_indices]
            # print("box_regression_pos:", box_regression_pos.shape, pos_score_sig.shape, pos_box_reg)

            # print("pos_score_sig:", hard_pos_sig, pos_score_sig)
            # pick hard negative which takes 1/4
            neg_score_sig = score_sig[plabels != 1]
            neg_num = neg_score_sig.shape[0]
            hard_neg_num = int(neg_num / 4) + 1
            hn_vals, hn_indices = torch.topk(neg_score_sig, hard_neg_num, dim=0)
            # hard_neg_sig = neg_score_sig[hn_indices]

            neg_label = plabels[plabels != 1]
            neg_label = neg_label[hn_indices]
            neg_logits = cls_logits[plabels != 1]
            neg_logits = neg_logits[hn_indices]

            hard_labels = torch.cat([pos_label, neg_label], dim=0)
            hard_logits = torch.cat([pos_logits, neg_logits], dim=0)

            ohem_box_loss = smooth_l1_loss(
                pos_box_reg,
                pos_box_target,
                beta=1.0 / 9,
                size_average=False,
            ) / float(hard_pos_num + hard_neg_num)

            ohem_objectness_loss = F.binary_cross_entropy_with_logits(
                hard_logits, hard_labels.to(hard_logits.device)
            )

            return ohem_objectness_loss, ohem_box_loss

        else:
            box_loss = smooth_l1_loss(
                box_regression_pos,
                regression_targets_pos,
                beta=1.0 / 9,
                size_average=False,
            ) / (sampled_inds.numel())

            score = objectness[sampled_inds]
            plabels = plabels
            objectness_loss = F.binary_cross_entropy_with_logits(
                score, plabels.to(score.device)
            )

            return objectness_loss, box_loss


def make_rpn_loss_evaluator(cfg, box_coder):
    matcher = Matcher(
        cfg.MODEL.RPN.FG_IOU_THRESHOLD,
        cfg.MODEL.RPN.BG_IOU_THRESHOLD,
        allow_low_quality_matches=True,
    )

    fg_bg_sampler = BalancedPositiveNegativeSampler(
        cfg.MODEL.RPN.BATCH_SIZE_PER_IMAGE, cfg.MODEL.RPN.POSITIVE_FRACTION
    )

    edge_punished = cfg.MODEL.EDGE_PUNISHED

    loss_evaluator = RPNLossComputation(matcher, fg_bg_sampler, box_coder, edge_punished)
    return loss_evaluator
