from torch import Tensor, rand
import torch.nn
import torch.nn.functional

from kge import Config, Dataset
from kge.job import Job
from kge.model import KgeEmbedder
from kge.misc import round_to_points

from typing import List
from os import path

from time import time

class LookupEmbedder(KgeEmbedder):
    def __init__(
        self,
        config: Config,
        dataset: Dataset,
        configuration_key: str,
        vocab_size: int,
        init_for_load_only=False,
    ):
        super().__init__(
            config, dataset, configuration_key, init_for_load_only=init_for_load_only
        )

        # read config
        self.normalize_p = self.get_option("normalize.p")
        self.space = self.check_option("space", ["euclidean", "complex"])

        # n3 is only accepted when space is complex
        if self.space == "complex":
            self.regularize = self.check_option("regularize", ["", "lp", "n3"])
        else:
            self.regularize = self.check_option("regularize", ["", "lp"])

        self.sparse = self.get_option("sparse")
        self.config.check("train.trace_level", ["batch", "epoch"])
        self.vocab_size = vocab_size

        round_embedder_dim_to = self.get_option("round_dim_to")
        if len(round_embedder_dim_to) > 0:
            self.dim = round_to_points(round_embedder_dim_to, self.dim)

        self._embeddings = torch.nn.Embedding(
            self.vocab_size, self.dim, sparse=self.sparse,
        )

        # Kazemi OOS parameters
        self.psi = self.get_option("psi")
        if self.psi <= 0:
            self.psi = None
        else:
            self.half_psi = self.psi / 2
            self.neighbour_edge_path = self.get_option("neighbour_edgelist_file")
            neighbour_edgelist = torch.load(path.join(self.dataset.folder, self.neighbour_edge_path))
            self.neighbour_adj = torch.sparse_coo_tensor(
                neighbour_edgelist.T,
                size = (self.dataset.num_entities(), self.dataset.num_entities()), 
                values=torch.ones(len(neighbour_edgelist))
            ).to(self._embeddings.weight.device)
            self.agg_candidates = set(neighbour_edgelist.numpy().T[0])
            self.config.log(f'Found psi value: {self.psi}')
            self.config.log(f'Created sparse neighbour adjacency of shape: {self.neighbour_adj.shape}')

        if not init_for_load_only:
            # initialize weights
            self.initialize(self._embeddings.weight.data)
            self._normalize_embeddings()

        # TODO handling negative dropout because using it with ax searches for now
        dropout = self.get_option("dropout")
        if dropout < 0:
            if config.get("job.auto_correct"):
                config.log(
                    "Setting {}.dropout to 0., "
                    "was set to {}.".format(configuration_key, dropout)
                )
                dropout = 0
        self.dropout = torch.nn.Dropout(dropout)

    def _normalize_embeddings(self):
        if self.normalize_p > 0:
            with torch.no_grad():
                self._embeddings.weight.data = torch.nn.functional.normalize(
                    self._embeddings.weight.data, p=self.normalize_p, dim=-1
                )

    def prepare_job(self, job: Job, **kwargs):
        from kge.job import TrainingJob

        super().prepare_job(job, **kwargs)
        if self.normalize_p > 0 and isinstance(job, TrainingJob):
            # just to be sure it's right initially
            job.pre_run_hooks.append(lambda job: self._normalize_embeddings())

            # normalize after each batch
            job.post_batch_hooks.append(lambda job: self._normalize_embeddings())

    @torch.no_grad()
    def init_pretrained(self, pretrained_embedder: KgeEmbedder) -> None:
        (
            self_intersect_ind,
            pretrained_intersect_ind,
        ) = self._intersect_ids_with_pretrained_embedder(pretrained_embedder)
        self._embeddings.weight[
            torch.from_numpy(self_intersect_ind)
            .to(self._embeddings.weight.device)
            .long()
        ] = pretrained_embedder.embed(torch.from_numpy(pretrained_intersect_ind)).to(
            self._embeddings.weight.device
        )

    def _aggregate(self, ind):
        neighbours = self.neighbour_adj[ind]._indices().flatten().to(self._embeddings.weight.device)
        neighbour_vecs = self.embed(neighbours)
        aggs = neighbour_vecs.mean(dim=0)
        return aggs

    def aggregate_bunch(self, indexes: Tensor) -> Tensor:
        agg_bools = [ind.item() in self.agg_candidates for ind in indexes]
        if not any(agg_bools):
            return self.embed(indexes)
        else:
            # Load regular embeddings
            embeds = self.embed(indexes)

            # Perform aggregations
            #aggregations = agg_nodes.apply_(self._aggregate) # Would rather use .apply to aggregate but doesnt work on gpu
            agg_nodes = indexes[agg_bools]
            aggregations = torch.concat(
                [self._aggregate(node_to_agg) for node_to_agg in agg_nodes]
            ).view(
                sum(agg_bools), embeds.shape[-1]
            )

            # Insert aggregations in place of embeds for target nodes
            embeds[agg_bools] = aggregations

            return self._postprocess(embeds)

    def embed(self, indexes: Tensor) -> Tensor:
        return self._postprocess(self._embeddings(indexes.long()))

    def embed_all(self) -> Tensor:
        return self._postprocess(self._embeddings_all())

    def _postprocess(self, embeddings: Tensor) -> Tensor:
        if self.dropout.p > 0:
            embeddings = self.dropout(embeddings)
        return embeddings

    def _embeddings_all(self) -> Tensor:
        return self._embeddings(
            torch.arange(
                self.vocab_size, dtype=torch.long, device=self._embeddings.weight.device
            )
        )

    def _get_regularize_weight(self) -> Tensor:
        return self.get_option("regularize_weight")

    def _abs_complex(self, parameters) -> Tensor:
        parameters_re, parameters_im = (t.contiguous() for t in parameters.chunk(2, dim=1))
        parameters = torch.sqrt(parameters_re ** 2 + parameters_im ** 2 + 1e-14) # + 1e-14 to avoid NaN: https://github.com/lilanxiao/Rotated_IoU/issues/20
        return parameters

    def penalty(self, **kwargs) -> List[Tensor]:
        # TODO factor out to a utility method
        result = super().penalty(**kwargs)
        if self.regularize == "" or self.get_option("regularize_weight") == 0.0:
            pass
        elif self.regularize in ["lp", 'n3']:
            if self.regularize == "n3":
                p = 3
            else:
                p = (
                    self.get_option("regularize_args.p")
                    if self.has_option("regularize_args.p")
                    else 2
                )
            regularize_weight = self._get_regularize_weight()
            if not self.get_option("regularize_args.weighted"):
                # unweighted Lp regularization
                parameters = self._embeddings_all()
                if self.regularize == "n3" and self.space == 'complex':
                    parameters = self._abs_complex(parameters)
                result += [
                    (
                        f"{self.configuration_key}.L{p}_penalty",
                        (regularize_weight / p * parameters.norm(p=p) ** p).sum(),
                    )
                ]
            else:
                # weighted Lp regularization
                unique_indexes, counts = torch.unique(
                    kwargs["indexes"], return_counts=True
                )
                parameters = self._embeddings(unique_indexes)

                if self.regularize == "n3" and self.space == 'complex':
                    parameters = self._abs_complex(parameters)

                if (p % 2 == 1) and (self.regularize != "n3"):
                    parameters = torch.abs(parameters)
                result += [
                    (
                        f"{self.configuration_key}.L{p}_penalty",
                        (
                            regularize_weight
                            / p
                            * (parameters ** p * counts.float().view(-1, 1))
                        ).sum()
                        # In contrast to unweighted Lp regularization, rescaling by
                        # number of triples/indexes is necessary here so that penalty
                        # term is correct in expectation
                        / len(kwargs["indexes"]),
                    )
                ]
        else:  # unknown regularization
            raise ValueError(f"Invalid value regularize={self.regularize}")

        return result
