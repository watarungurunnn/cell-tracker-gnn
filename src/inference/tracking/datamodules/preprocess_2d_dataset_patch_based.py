import os
import os.path as op
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd
import torch
from preprocess_dataset import PreprocessDataset
from skimage.measure import regionprops

from src_metric_learning.modules.resnet_2d.resnet import (
    MLP, set_model_architecture)


class PreprocessPatchBasedDataset(PreprocessDataset):

    def __init__(self,
                 path: str,
                 path_result: str,
                 type_img: str,
                 type_masks: str,
                 ndim: int,
                 ):
        assert ndim == 2, f'dimension of dataset has to be 2(D).'
        super().__init__(path, path_result, type_img, type_masks, ndim)

    def _extract_patch(self, img, bbox):
        kernel = (64, 64)
        min_row_bb, min_col_bb, max_row_bb, max_col_bb = bbox
        d_row = kernel[0] - (max_row_bb - min_row_bb)
        d_col = kernel[1] - (max_col_bb - min_col_bb)
        shape_row, shape_col = img.shape

        # find cols min max
        if min_row_bb < (d_row // 2):
            min_row = 0
            max_row = kernel[0]
        elif (max_row_bb + d_row // 2) > shape_row:
            min_row = shape_row - kernel[0] - 1
            max_row = shape_row - 1
        else:
            min_row = min_row_bb - d_row // 2
            max_row = max_row_bb + d_row // 2
            if (d_row // 2) * 2 != d_row:
                if min_row == 0:
                    max_row += 1
                else:
                    min_row -= 1

        # find cols min max
        if min_col_bb < (d_col // 2):
            min_col = 0
            max_col = kernel[1]
        elif (max_col_bb + d_col // 2) > shape_col:
            min_col = shape_col - kernel[1] - 1
            max_col = shape_col - 1
        else:
            min_col = min_col_bb - d_col // 2
            max_col = max_col_bb + d_col // 2
            if (d_col // 2) * 2 != d_col:
                if min_col == 0:
                    max_col += 1
                else:
                    min_col -= 1


        patch = img[min_row: max_row, min_col: max_col]
        assert patch.shape == kernel, f"patch.shape: {patch.shape} , " \
            f"[min_row, max_row, min_col,  max_col] = {[min_row, max_row, min_col, max_col]}"
        return min_row, max_row, min_col, max_col

    def _extract_freature_metric_learning(self, bbox, img, seg_mask, ind, normalize_type='MinMax_all'):
        min_row_bb, min_col_bb, max_row_bb, max_col_bb = bbox
        img_patch = img[min_row_bb:max_row_bb, min_col_bb:max_col_bb]
        msk_patch = seg_mask[min_row_bb:max_row_bb, min_col_bb:max_col_bb] != ind
        assert normalize_type == 'MinMax_all'
        if normalize_type != 'MinMax_all':
            img_patch[msk_patch] = self.__pad_value
        img_patch = img_patch.astype(np.float32)

        if normalize_type == 'MinMax_all':
            assert img_patch.shape == (self.__roi_model['row'], self.__roi_model['col']), \
                f"Problem! {img_patch.shape} should be {(self.__roi_model['row'], self.__roi_model['col'])}"
            img_patch = (img_patch - self.__min_cell) / (self.__max_cell - self.__min_cell)
            img = img_patch.squeeze()
        else:
            assert False, "Not supported this type of normalization"
        assert img.min() >= 0 and img.max() <= 1, "Problem! Image values are not in range!"
        img = torch.from_numpy(img).float()
        with torch.no_grad():
            embedded_img = self.__embedder(self.__trunk(img[None, None, ...]))

        return embedded_img.numpy().squeeze()


    def preprocess_features_w_metric_learning(self, dict_path):
        dict_params = torch.load(dict_path)
        kernel = (64, 64)
        self.__min_cell = dict_params['min_all']
        self.__max_cell = dict_params['max_all']
        self.__roi_model = dict_params['roi']
        # models params
        model_name = dict_params['model_name']
        mlp_dims = dict_params['mlp_dims']
        mlp_normalized_features = dict_params['mlp_normalized_features']
        # models state_dict
        trunk_state_dict = dict_params['trunk_state_dict']
        embedder_state_dict = dict_params['embedder_state_dict']

        trunk = set_model_architecture(model_name)
        trunk.load_state_dict(trunk_state_dict)
        self.__trunk = trunk
        self.__trunk.eval()

        embedder = MLP(mlp_dims, normalized_feat=mlp_normalized_features)
        embedder.load_state_dict(embedder_state_dict)
        self.__embedder = embedder
        self.__embedder.eval()

        cols = ["id", "seg_label",
                "frame_num",
                "min_row_bb", "min_col_bb", "max_row_bb", "max_col_bb",
                "centroid_row", "centroid_col",
                "max_intensity", "mean_intensity", "min_intensity"
                ]

        cols_resnet = [f'feat_{i}' for i in range(mlp_dims[-1])]
        cols += cols_resnet

        for ind_data in range(self.__len__()):
            img, result, im_path, result_path = self[ind_data]
            mask_path = result_path
            mask = result
            im_num, mask_num = im_path.split(".")[-2][-3:], mask_path.split(".")[-2][-3:]

            assert im_num == mask_num, f"Image number ({im_num}) is not equal to mask number ({mask_num})"
            im_num_int = int(im_num)
            labels_mask = np.unique(mask)

            num_labels = labels_mask.shape[0]
            if 0 in labels_mask:
                num_labels = num_labels - 1
                flag_zero_label = True

            df = pd.DataFrame(index=range(num_labels), columns=cols)

            for ind, cell_id in enumerate(labels_mask):
                # Color 0 is assumed to be background or artifacts
                if cell_id == 0:
                    continue
                if flag_zero_label:
                    ind_df = ind - 1

                df.loc[ind_df, "id"] = cell_id

                # extracting statistics using regionprops
                properties = regionprops(np.uint8(mask == cell_id), img)[0]

                centroid_row, centroid_col = properties.centroid[0].round().astype(np.int16), \
                                             properties.centroid[1].round().astype(np.int16)

                min_row_bb, min_col_bb, max_row_bb, max_col_bb = properties.bbox
                if max_row_bb - min_row_bb < kernel[0]:
                    if max_col_bb - min_col_bb < kernel[1]:
                        min_row, max_row, min_col, max_col = self._extract_patch(img, properties.bbox)
                    else:
                        min_col = max(centroid_col - 1, 0)
                        max_col = min(centroid_col + 1, mask.shape[1] - 1)
                        min_row, max_row, min_col, max_col = self._extract_patch(img, (min_row_bb, min_col, max_row_bb, max_col))
                else:
                    if max_col_bb - min_col_bb < kernel[1]:
                        min_row = max(centroid_row - 1, 0)
                        max_row = min(centroid_row + 1, mask.shape[0] - 1)
                        min_row, max_row, min_col, max_col = self._extract_patch(img, (min_row, min_col_bb, max_row, max_col_bb))
                    else:
                        min_row = max(centroid_row - 1, 0)
                        max_row = min(centroid_row + 1, mask.shape[0] - 1)
                        min_col = max(centroid_col - 1, 0)
                        max_col = min(centroid_col + 1, mask.shape[1] - 1)
                        min_row, max_row, min_col, max_col = self._extract_patch(img, (min_row, min_col, max_row, max_col))

                bbox = (min_row, min_col, max_row, max_col)
                embedded_feat = self._extract_freature_metric_learning(bbox, img.copy(), mask.copy(), cell_id)
                df.loc[ind_df, cols_resnet] = embedded_feat
                df.loc[ind_df, "seg_label"] = cell_id

                df.loc[ind_df, "min_row_bb"], df.loc[ind_df, "min_col_bb"], \
                df.loc[ind_df, "max_row_bb"], df.loc[ind_df, "max_col_bb"] = min_row, min_col, max_row, max_col

                df.loc[ind_df, "centroid_row"], df.loc[ind_df, "centroid_col"] = \
                    properties.centroid[0].round().astype(np.int16), \
                    properties.centroid[1].round().astype(np.int16)

                df.loc[ind_df, "max_intensity"], df.loc[ind_df, "mean_intensity"], df.loc[ind_df, "min_intensity"] = \
                    properties.max_intensity, properties.mean_intensity, properties.min_intensity

            df.loc[:, "frame_num"] = im_num_int

            if df.isnull().values.any():
                warnings.warn("Pay Attention! there are Nan values!")

            yield df, im_num
