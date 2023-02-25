import os
import sys

sys.path.append(os.pardir)

import numpy as np
from tree_party import Party


class XGBoostParty(Party):
    def __init__(
        self,
        x,
        num_classes,
        feature_id,
        party_id,
        min_leaf,
        subsample_cols,
        num_precentile_bin,
        use_missing_value=False,
        seed=0,
    ):
        super().__init__(
            x,
            num_classes,
            feature_id,
            party_id,
            min_leaf,
            subsample_cols,
            use_missing_value,
            seed,
        )
        self.num_precentile_bin = num_precentile_bin

    def get_threshold_candidates(self, x_col):
        if len(x_col) > self.num_precentile_bin:
            return super().get_threshold_candidates(
                np.quantile(
                    x_col,
                    [
                        i / self.num_precentile_bin
                        for i in range(1, self.num_precentile_bin + 1)
                    ],
                )
            )
        else:
            return super().get_threshold_candidates(x_col)

    def greedy_search_split(self, gradient, hessian, y, idxs):
        if self.use_missing_value:
            num_thresholds = self.subsample_col_count * 2
        else:
            num_thresholds = self.subsample_col_count

        split_candidates_grad_hess = [[] for _ in range(num_thresholds)]
        temp_thresholds = [[] for _ in range(num_thresholds)]

        row_count = len(idxs)
        grad_dim = len(gradient[0])

        for i in range(self.subsample_col_count):
            k = self.temp_column_subsample[i]
            x_col = []

            not_missing_values_count = 0
            missing_values_count = 0
            for r in range(row_count):
                if self.x[idxs[r]][k] is not None:
                    x_col.append(self.x[idxs[r]][k])
                    not_missing_values_count += 1
                else:
                    missing_values_count += 1

            x_col_idxs = np.argsort(x_col)
            x_col.sort()

            percentiles = self.get_threshold_candidates(x_col)

            current_min_idx = 0
            cumulative_left_size = 0
            for p in range(len(percentiles)):
                temp_grad = np.zeros(grad_dim)
                temp_hess = np.zeros(grad_dim)
                temp_left_y_class_cnt = np.zeros(self.num_classes)
                temp_left_size = 0

                for r in range(current_min_idx, not_missing_values_count):
                    if x_col[r] <= percentiles[p]:
                        for c in range(grad_dim):
                            temp_grad[c] += gradient[idxs[x_col_idxs[r]]][c]
                            temp_hess[c] += hessian[idxs[x_col_idxs[r]]][c]
                        temp_left_size += 1
                        temp_left_y_class_cnt[int(y[idxs[x_col_idxs[r]]])] += 1
                        cumulative_left_size += 1
                    else:
                        current_min_idx = r
                        break
                if (
                    cumulative_left_size >= self.min_leaf
                    and row_count - cumulative_left_size >= self.min_leaf
                ):
                    split_candidates_grad_hess[i].append(
                        (temp_grad, temp_hess, temp_left_size, temp_left_y_class_cnt)
                    )
                    temp_thresholds[i].append(percentiles[p])

            if self.use_missing_value:
                current_max_idx = not_missing_values_count - 1
                cumulative_right_size = 0

                for p in range(len(percentiles) - 1, 0, -1):
                    temp_grad = np.zeros(grad_dim)
                    temp_hess = np.zeros(grad_dim)
                    temp_left_y_class_cnt = np.zeros(self.num_classes)
                    temp_left_size = 0

                    for r in range(current_max_idx, 0, -1):
                        if x_col[r] <= percentiles[p]:
                            for c in range(grad_dim):
                                temp_grad[c] += gradient[idxs[x_col_idxs[r]]][c]
                                temp_hess[c] += hessian[idxs[x_col_idxs[r]]][c]
                            temp_left_size += 1
                            temp_left_y_class_cnt[int(y[idxs[x_col_idxs[r]]])] += 1
                            cumulative_right_size += 1
                        else:
                            current_max_idx = r
                            break
                    if (
                        cumulative_right_size >= self.min_leaf
                        and row_count - cumulative_right_size >= self.min_leaf
                    ):
                        split_candidates_grad_hess[i + self.subsample_col_count].append(
                            (
                                temp_grad,
                                temp_hess,
                                temp_left_size,
                                temp_left_y_class_cnt,
                            )
                        )
                        temp_thresholds[i + self.subsample_col_count].append(
                            percentiles[p]
                        )

        return split_candidates_grad_hess
