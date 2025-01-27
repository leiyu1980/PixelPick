import pickle as pkl

import numpy as np
import torch
import torch.nn.functional as F
from tqdm import tqdm


class QuerySelector:
    def __init__(self, args, dataloader, device=torch.device("cuda:0")):
        self.dataset_name = args.dataset_name
        self.dataloader = dataloader
        self.debug = args.debug
        self.device = device
        self.dir_checkpoints = f"{args.dir_root}/checkpoints/{args.experim_name}"
        self.ignore_index = args.ignore_index
        self.mc_n_steps = args.mc_n_steps
        self.n_classes = args.n_classes
        self.n_pixels_by_us = args.n_pixels_by_us
        self.network_name = args.network_name
        self.query_stats = QueryStats(args)
        self.query_strategy = args.query_strategy
        self.reverse_order = args.reverse_order
        self.stride_total = args.stride_total
        self.top_n_percent = args.top_n_percent
        self.uncertainty_sampler = UncertaintySampler(args.query_strategy)
        self.use_mc_dropout = args.use_mc_dropout
        self.vote_type = args.vote_type

    def _select_queries(self, uc_map):
        h, w = uc_map.shape[-2:]
        uc_map = uc_map.flatten()
        k = int(h * w * self.top_n_percent) if self.top_n_percent > 0. else self.n_pixels_by_us

        if self.reverse_order:
            assert self.top_n_percent > 0.
            ind_queries = np.random.choice(range(h * w), k, False)
            sampling_mask = np.zeros((h * w), dtype=np.bool)
            sampling_mask[ind_queries] = True
            sampling_mask = torch.tensor(sampling_mask, dtype=torch.bool, device=self.device)

            if self.query_strategy in ["entropy", "least_confidence"]:
                uc_map[~sampling_mask] = 0.
            else:
                uc_map[~sampling_mask] = 1.0

            ind_queries = uc_map.topk(k=self.n_pixels_by_us,
                                      dim=0,
                                      largest=self.query_strategy in ["entropy",
                                                                      "least_confidence"]).indices.cpu().numpy()

        else:
            ind_queries = uc_map.topk(k=k,
                                      dim=0,
                                      largest=self.query_strategy in ["entropy", "least_confidence"]).indices.cpu().numpy()
            if self.top_n_percent > 0.:
                ind_queries = np.random.choice(ind_queries, self.n_pixels_by_us, False)

        query = np.zeros((h * w), dtype=np.bool)
        query[ind_queries] = True
        query = query.reshape((h, w))
        return query

    def __call__(self, nth_query, model, prototypes=None):
        queries = self.dataloader.dataset.queries

        model.eval()
        if self.use_mc_dropout:
            model.turn_on_dropout()

        print(f"Choosing pixels by {self.query_strategy}")
        list_queries, n_pixels = list(), 0
        with torch.no_grad():
            for batch_ind, dict_data in tqdm(enumerate(self.dataloader)):
                x = dict_data['x'].to(self.device)
                y = dict_data['y'].squeeze(dim=0).numpy()   # h x w
                mask = queries[batch_ind]
                mask_void = (y == self.ignore_index)  # h x w
                h, w = x.shape[2:]

                # voc
                if self.dataset_name == "voc":
                    from math import ceil
                    pad_h = ceil(h / self.stride_total) * self.stride_total - h  # x.shape[2]
                    pad_w = ceil(w / self.stride_total) * self.stride_total - w  # x.shape[3]
                    x = F.pad(x, pad=(0, pad_w, 0, pad_h), mode='reflect')

                # get uncertainty map
                if self.use_mc_dropout:
                    uc_map = torch.zeros((h, w)).to(self.device)  # (h, w)
                    prob = torch.zeros((x.shape[0], self.n_classes, h, w)).to(self.device)  # b x c x h x w
                    # repeat for mc_n_steps times - set to 20 as a default
                    for step in range(self.mc_n_steps):
                        prob_ = F.softmax(model(x)["pred"], dim=1)[:, :, :h, :w]
                        uc_map_ = self.uncertainty_sampler(prob_).squeeze(dim=0)  # h x w
                        uc_map += uc_map_
                        prob += prob_
                    up_map = up_map / self.mc_n_steps
                    prob = prob / self.mc_n_steps

                else:
                    prob = F.softmax(model(x)["pred"][:, :, :h, :w], dim=1)

                    uc_map = self.uncertainty_sampler(prob).squeeze(dim=0)  # h x w

                # exclude pixels that are already annotated, belong to the void category
                uc_map[mask] = 0.0 if self.query_strategy in ["entropy", "least_confidence"] else 1.0
                uc_map[mask_void] = 0.0 if self.query_strategy in ["entropy", "least_confidence"] else 1.0

                # select queries
                query = self._select_queries(uc_map)
                list_queries.append(query)
                n_pixels += query.sum()

                self.query_stats.update(query, y, prob)

        self.query_stats.save(nth_query)

        assert len(list_queries) > 0, f"no queries are chosen!"
        queries = np.stack(list_queries, axis=0) if self.dataset_name != "voc" else list_queries
        print(f"{n_pixels} labelled pixels  are chosen by {self.query_strategy} strategy")

        # Update labels for query dataloader. Note that this does not update labels for training dataloader.
        self.dataloader.dataset.label_queries(queries, nth_query + 1)
        return queries


class UncertaintySampler:
    def __init__(self, query_strategy):
        self.query_strategy = query_strategy

    @staticmethod
    def _entropy(prob):
        return (-prob * torch.log(prob)).sum(dim=1)  # b x h x w

    @staticmethod
    def _least_confidence(prob):
        return 1.0 - prob.max(dim=1)[0]  # b x h x w

    @staticmethod
    def _margin_sampling(prob):
        top2 = prob.topk(k=2, dim=1).values  # b x k x h x w
        return (top2[:, 0, :, :] - top2[:, 1, :, :]).abs()  # b x h x w

    @staticmethod
    def _random(prob):
        b, _, h, w = prob.shape
        return torch.rand((b, h, w))

    def __call__(self, prob):
        return getattr(self, f"_{self.query_strategy}")(prob)


class QueryStats:
    def __init__(self, args):
        self.dir_checkpoints = f"{args.dir_root}/checkpoints/{args.experim_name}"
        self.list_entropy, self.list_n_unique_labels, self.list_spatial_coverage = list(), list(), list()
        self.dict_label_cnt = {l: 0 for l in range(args.n_classes)}

    def _count_labels(self, query, y):
        for l in y.flatten()[query.flatten()]:
            self.dict_label_cnt[l] += 1

    @staticmethod
    def _get_entropy(query, prob):
        ent_map = (-prob * torch.log(prob)).sum(dim=1).cpu().numpy()  # h x w
        pixel_entropy = ent_map.flatten()[query.flatten()]  # n_pixels_per_img
        return pixel_entropy.tolist()

    @staticmethod
    def _n_unique_labels(query, y):
        return len(set(y.flatten()[query.flatten()]))

    @staticmethod
    def _spatial_coverage(query):
        x_loc, y_loc = np.where(query)
        x_loc, y_loc = np.expand_dims(x_loc, axis=1), np.expand_dims(y_loc, axis=1)
        x_loc_t, y_loc_t = x_loc.transpose(), y_loc.transpose()
        dist = np.sqrt((x_loc - x_loc_t) ** 2 + (y_loc - y_loc_t) ** 2)
        try:
            dist = dist[~np.eye(dist.shape[0], dtype=np.bool)].reshape(dist.shape[0], -1).mean()
        except ValueError:
            return np.NaN
        return dist

    def save(self, nth_query):
        dict_stats = {
            "label_distribution": self.dict_label_cnt,
            "avg_entropy": np.mean(self.list_entropy),
            "avg_n_unique_labels": np.mean(self.list_n_unique_labels),
            "avg_spatial_coverage": np.mean(self.list_spatial_coverage)
        }

        for k, v in dict_stats.items():
            print(f"{k}: {v}")

        pkl.dump(dict_stats, open(f"{self.dir_checkpoints}/{nth_query}_query/query_stats.pkl", "wb"))

    def update(self, query, y, prob):
        # count labels
        self._count_labels(query, y)

        # entropy
        self.list_entropy.extend(self._get_entropy(query, prob))

        # n_unique_labels
        self.list_n_unique_labels.append(self._n_unique_labels(query, y))

        # spatial_coverage
        self.list_spatial_coverage.append(self._spatial_coverage(query))
        return
