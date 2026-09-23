from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

try:
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler
except ImportError:  # pragma: no cover - exercised when optional dependency is absent
    KMeans = None
    PCA = None
    StandardScaler = None

from trader_engine.core.config import StateConfig
from trader_engine.states.base import BaseStateModel
from trader_engine.states.utils import infer_state_family


@dataclass
class ClusterBundle:
    selected_features: list[str]
    feature_medians: pd.Series
    scaler: StandardScaler | None
    pca: PCA | None
    model: KMeans | None
    cluster_map: dict[int, int]
    cluster_sizes: pd.Series
    cluster_centers_original: pd.DataFrame
    transformed_dimensions: int


class KMeansStateModel(BaseStateModel):
    def __init__(self, state_config: StateConfig) -> None:
        self.state_config = state_config
        self.bundle: ClusterBundle | None = None

    def fit(self, frame: pd.DataFrame) -> "KMeansStateModel":
        self.bundle = fit_cluster_bundle(
            frame=frame,
            state_config=self.state_config,
            selected_features=self.state_config.cluster_features,
            requested_cluster_count=self.state_config.cluster_count,
        )
        return self

    def classify(self, frame: pd.DataFrame) -> pd.DataFrame:
        if self.bundle is None:
            raise ValueError("KMeansStateModel must be fitted before classify().")
        classified = frame.copy()
        cluster_ids = predict_cluster_ids(frame=frame, bundle=self.bundle)
        classified["state_cluster"] = [f"cluster_{cluster:02d}" for cluster in cluster_ids]
        classified["state"] = classified["state_cluster"]
        classified["state_family"] = infer_state_family(classified)
        return classified

    def model_artifacts(self) -> dict[str, pd.DataFrame | dict]:
        if self.bundle is None:
            return {}
        sizes = (
            self.bundle.cluster_sizes.rename_axis("cluster_id")
            .reset_index(name="observations")
            .assign(cluster=lambda frame: frame["cluster_id"].map(lambda value: f"cluster_{int(value):02d}"))
        )
        merge_map = pd.DataFrame(
            [
                {"original_cluster_id": original, "cluster": f"cluster_{mapped:02d}"}
                for original, mapped in sorted(self.bundle.cluster_map.items())
            ]
        )
        artifacts: dict[str, pd.DataFrame | dict] = {
            "cluster_centers": self.bundle.cluster_centers_original.reset_index(drop=True),
            "cluster_sizes": sizes[["cluster", "observations"]],
            "cluster_merge_map": merge_map,
            "cluster_model_settings": pd.DataFrame(
                [
                    {"parameter": "cluster_count_requested", "value": self.state_config.cluster_count},
                    {"parameter": "cluster_min_size", "value": self.state_config.cluster_min_size},
                    {"parameter": "cluster_merge_small_clusters", "value": self.state_config.cluster_merge_small_clusters},
                    {
                        "parameter": "cluster_dimensionality_reduction",
                        "value": self.state_config.cluster_dimensionality_reduction,
                    },
                    {"parameter": "cluster_pca_components", "value": self.state_config.cluster_pca_components},
                    {"parameter": "transformed_dimensions", "value": self.bundle.transformed_dimensions},
                ]
            ),
        }
        if self.bundle.pca is not None:
            artifacts["pca_explained_variance"] = pd.DataFrame(
                {
                    "component": [f"pc_{index + 1}" for index in range(len(self.bundle.pca.explained_variance_ratio_))],
                    "explained_variance_ratio": self.bundle.pca.explained_variance_ratio_,
                }
            )
        return artifacts


def fit_cluster_bundle(
    frame: pd.DataFrame,
    state_config: StateConfig,
    selected_features: list[str],
    requested_cluster_count: int,
) -> ClusterBundle:
    _require_sklearn()
    features = [column for column in selected_features if column in frame.columns and frame[column].notna().any()]
    if not features:
        raise ValueError("No configured cluster_features were found in the feature frame.")

    training = frame[features].copy()
    medians = training.median().fillna(0.0)
    filled = training.fillna(medians).fillna(0.0)
    scaler = StandardScaler()
    scaled = scaler.fit_transform(filled)

    transformed = scaled
    pca_model: PCA | None = None
    if (
        state_config.cluster_dimensionality_reduction == "pca"
        and PCA is not None
        and state_config.cluster_pca_components
        and state_config.cluster_pca_components > 0
        and state_config.cluster_pca_components < transformed.shape[1]
    ):
        components = min(int(state_config.cluster_pca_components), transformed.shape[0], transformed.shape[1])
        if components >= 1:
            pca_model = PCA(n_components=components, random_state=state_config.cluster_random_state)
            transformed = pca_model.fit_transform(transformed)

    effective_cluster_count = _effective_cluster_count(
        requested=requested_cluster_count,
        sample_count=len(filled),
        min_size=state_config.cluster_min_size,
    )
    model: KMeans | None = None
    if effective_cluster_count <= 1:
        assignments = np.zeros(len(filled), dtype=int)
        cluster_map = {0: 0}
    else:
        model = KMeans(
            n_clusters=effective_cluster_count,
            random_state=state_config.cluster_random_state,
            n_init=state_config.cluster_n_init,
            max_iter=state_config.cluster_max_iter,
        )
        raw_assignments = model.fit_predict(transformed)
        if state_config.cluster_merge_small_clusters and state_config.cluster_min_size > 0:
            assignments, cluster_map = _merge_small_clusters(
                raw_assignments=raw_assignments,
                transformed=transformed,
                min_size=state_config.cluster_min_size,
            )
        else:
            assignments = raw_assignments
            cluster_map = {int(cluster): int(cluster) for cluster in sorted(pd.unique(raw_assignments))}
    unique_clusters = sorted(pd.unique(assignments))
    cluster_sizes = pd.Series(assignments).value_counts().sort_index()
    centers = (
        pd.DataFrame(filled, columns=features)
        .assign(final_cluster=assignments)
        .groupby("final_cluster")[features]
        .mean()
        .reindex(unique_clusters)
        .reset_index()
        .rename(columns={"final_cluster": "cluster_id"})
    )
    centers.insert(1, "cluster", centers["cluster_id"].map(lambda value: f"cluster_{int(value):02d}"))

    bundle = ClusterBundle(
        selected_features=features,
        feature_medians=medians,
        scaler=scaler,
        pca=pca_model,
        model=None if effective_cluster_count <= 1 else model,
        cluster_map=cluster_map,
        cluster_sizes=cluster_sizes,
        cluster_centers_original=centers[["cluster", *features]],
        transformed_dimensions=int(transformed.shape[1]),
    )
    return bundle


def predict_cluster_ids(frame: pd.DataFrame, bundle: ClusterBundle) -> np.ndarray:
    filled = frame[bundle.selected_features].fillna(bundle.feature_medians).fillna(0.0)
    transformed = bundle.scaler.transform(filled) if bundle.scaler is not None else filled.to_numpy()
    if bundle.pca is not None:
        transformed = bundle.pca.transform(transformed)
    if bundle.model is None:
        raw = np.zeros(len(filled), dtype=int)
    else:
        raw = bundle.model.predict(transformed)
    return np.array([bundle.cluster_map.get(int(cluster), 0) for cluster in raw], dtype=int)


def _require_sklearn() -> None:
    if KMeans is None or StandardScaler is None:
        raise ImportError("Cluster-based state models require scikit-learn. Install `scikit-learn>=1.5`.")


def _effective_cluster_count(requested: int, sample_count: int, min_size: int) -> int:
    if sample_count <= 1:
        return 1
    cluster_count = max(int(requested), 1)
    if min_size > 0:
        cluster_count = min(cluster_count, max(sample_count // min_size, 1))
    return min(cluster_count, sample_count)


def _merge_small_clusters(raw_assignments: np.ndarray, transformed: np.ndarray, min_size: int) -> tuple[np.ndarray, dict[int, int]]:
    counts = pd.Series(raw_assignments).value_counts().sort_index()
    if counts.empty:
        return raw_assignments, {}
    final_assignments = raw_assignments.copy()
    centers = {int(cluster): transformed[raw_assignments == cluster].mean(axis=0) for cluster in counts.index}
    valid_clusters = set(counts[counts >= min_size].index.tolist())
    if not valid_clusters:
        dominant = int(counts.idxmax())
        valid_clusters = {dominant}

    cluster_map = {int(cluster): int(cluster) for cluster in counts.index}
    for cluster in counts[counts < min_size].index.tolist():
        cluster = int(cluster)
        if cluster in valid_clusters:
            continue
        candidates = [int(candidate) for candidate in valid_clusters if int(candidate) != cluster]
        if not candidates:
            target = int(max(valid_clusters, key=lambda item: counts.loc[item]))
        else:
            distances = {
                candidate: float(np.linalg.norm(centers[cluster] - centers[candidate]))
                for candidate in candidates
            }
            target = min(distances, key=distances.get)
        final_assignments[final_assignments == cluster] = target
        cluster_map[cluster] = target
        valid_clusters.add(target)

    remap = {int(cluster): index for index, cluster in enumerate(sorted(pd.unique(final_assignments)))}
    remapped_assignments = np.array([remap[int(cluster)] for cluster in final_assignments], dtype=int)
    remapped_map = {int(original): remap[int(mapped)] for original, mapped in cluster_map.items()}
    return remapped_assignments, remapped_map
