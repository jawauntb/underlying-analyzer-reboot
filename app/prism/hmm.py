"""Pure-numpy Gaussian hidden Markov model.

Implements the model used by Wang, Lin and Mikhelson (2020), *Regime-Switching
Factor Investing with Hidden Markov Models* (J. Risk Financial Manag. 13, 311):
a continuous Gaussian HMM with **full** per-state covariance matrices, fitted by
Baum-Welch expectation maximisation.  The paper leans on ``hmmlearn``; this
module reimplements the same estimator in numpy only, because the engine is not
allowed to pull heavy scientific dependencies.

Everything runs in log space (``logsumexp`` reductions) so long daily sequences
do not underflow, and every covariance update is regularised so a collapsing
state cannot produce a singular matrix.

The module is deliberately free of pandas and of any Prism-specific concept:
it consumes a ``(n_samples, n_features)`` float array and returns fitted
parameters.  ``app/prism/regimes.py`` owns the financial interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

__all__ = [
    "GaussianHMM",
    "HMMError",
    "expected_durations",
    "filtered_posteriors",
    "filtered_states",
    "fit_gaussian_hmm",
    "forward_backward",
    "log_gaussian_pdf",
    "posterior_states",
    "score",
    "stationary_distribution",
    "viterbi",
]

_LOG_ZERO = -1.0e300
_DEFAULT_SEED = 20260901


class HMMError(ValueError):
    """Raised when the observation matrix cannot support the requested model."""


@dataclass(frozen=True)
class GaussianHMM:
    """A fitted Gaussian HMM with full covariance matrices.

    Attributes
    ----------
    start_prob:
        ``(n_states,)`` initial state distribution ``pi``.
    transition:
        ``(n_states, n_states)`` row-stochastic transition matrix ``A``;
        ``transition[i, j]`` is ``P(state_{t+1} = j | state_t = i)``.
    means:
        ``(n_states, n_features)`` emission means ``mu``.
    covariances:
        ``(n_states, n_features, n_features)`` full emission covariances.
    log_likelihood:
        Log likelihood of the training sequence under the final parameters.
    n_iter_run / converged / log_likelihood_trace:
        EM diagnostics.
    """

    start_prob: FloatArray
    transition: FloatArray
    means: FloatArray
    covariances: FloatArray
    log_likelihood: float
    n_iter_run: int
    converged: bool
    log_likelihood_trace: tuple[float, ...]

    @property
    def n_states(self) -> int:
        return int(self.means.shape[0])

    @property
    def n_features(self) -> int:
        return int(self.means.shape[1])

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view of the fitted parameters."""
        return {
            "n_states": self.n_states,
            "n_features": self.n_features,
            "start_prob": [float(value) for value in self.start_prob],
            "transition": [[float(value) for value in row] for row in self.transition],
            "means": [[float(value) for value in row] for row in self.means],
            "covariances": [
                [[float(value) for value in row] for row in matrix] for matrix in self.covariances
            ],
            "log_likelihood": float(self.log_likelihood),
            "n_iter_run": int(self.n_iter_run),
            "converged": bool(self.converged),
        }


def _as_matrix(observations: npt.ArrayLike) -> FloatArray:
    array = np.asarray(observations, dtype=np.float64)
    if array.ndim == 1:
        array = array.reshape(-1, 1)
    if array.ndim != 2:
        raise HMMError("observations must be a 1-D or 2-D array")
    if array.shape[0] == 0:
        raise HMMError("observations must contain at least one sample")
    if not np.all(np.isfinite(array)):
        raise HMMError("observations must be finite (drop NaN/inf before fitting)")
    return array


def _logsumexp(values: FloatArray, axis: int | None = None) -> Any:
    """Numerically stable ``log(sum(exp(values)))``."""
    maximum = np.max(values, axis=axis, keepdims=True)
    maximum = np.where(np.isfinite(maximum), maximum, 0.0)
    shifted = np.exp(values - maximum)
    total = np.log(np.sum(shifted, axis=axis, keepdims=True)) + maximum
    if axis is None:
        return float(total.reshape(()))
    return np.squeeze(total, axis=axis)


def _safe_log(values: FloatArray) -> FloatArray:
    out = np.full(values.shape, _LOG_ZERO, dtype=np.float64)
    positive = values > 0.0
    out[positive] = np.log(values[positive])
    return out


def _regularise(cov: FloatArray, floor: float) -> FloatArray:
    """Return a positive-definite copy of ``cov``.

    Adds an escalating ridge until the Cholesky factorisation succeeds; this is
    the guard against a state whose posterior mass collapses onto a handful of
    identical observations.
    """
    n = cov.shape[0]
    eye = np.eye(n, dtype=np.float64)
    scale = max(float(np.trace(cov)) / max(n, 1), floor)
    candidate = 0.5 * (cov + cov.T) + eye * floor * max(scale, 1.0)
    jitter = floor * max(scale, 1.0)
    for _ in range(12):
        try:
            np.linalg.cholesky(candidate)
        except np.linalg.LinAlgError:
            candidate = candidate + eye * jitter
            jitter *= 10.0
            continue
        return candidate
    return eye * max(scale, floor)


def log_gaussian_pdf(observations: npt.ArrayLike, mean: FloatArray, cov: FloatArray) -> FloatArray:
    """Log density of a multivariate normal evaluated at every row.

    Returns a ``(n_samples,)`` array.  Uses a Cholesky solve rather than an
    explicit inverse so ill-conditioned covariances degrade gracefully.
    """
    matrix = _as_matrix(observations)
    n_features = matrix.shape[1]
    regular = _regularise(np.asarray(cov, dtype=np.float64), 1e-10)
    chol = np.linalg.cholesky(regular)
    delta = matrix - np.asarray(mean, dtype=np.float64).reshape(1, -1)
    solved = np.linalg.solve(chol, delta.T)
    quad = np.sum(solved**2, axis=0)
    log_det = 2.0 * float(np.sum(np.log(np.diag(chol))))
    constant = n_features * float(np.log(2.0 * np.pi))
    return np.asarray(-0.5 * (constant + log_det + quad), dtype=np.float64)


def _emission_log_prob(
    model_means: FloatArray, model_covs: FloatArray, obs: FloatArray
) -> FloatArray:
    n_states = model_means.shape[0]
    out = np.empty((obs.shape[0], n_states), dtype=np.float64)
    for state in range(n_states):
        out[:, state] = log_gaussian_pdf(obs, model_means[state], model_covs[state])
    return out


def _kmeans_init(
    obs: FloatArray, n_states: int, *, seed: int, n_iter: int = 50
) -> tuple[FloatArray, IntArray]:
    """Deterministic k-means++ seeding followed by Lloyd iterations.

    Deterministic given ``seed``: the RNG is a fresh ``default_rng(seed)``.
    """
    rng = np.random.default_rng(seed)
    n_samples = obs.shape[0]
    if n_samples < n_states:
        raise HMMError(f"need at least {n_states} samples to initialise {n_states} states")

    centres = np.empty((n_states, obs.shape[1]), dtype=np.float64)
    first = int(rng.integers(0, n_samples))
    centres[0] = obs[first]
    closest = np.sum((obs - centres[0]) ** 2, axis=1)
    for index in range(1, n_states):
        total = float(np.sum(closest))
        if total <= 0.0 or not np.isfinite(total):
            centres[index] = obs[int(rng.integers(0, n_samples))]
        else:
            probabilities = closest / total
            pick = int(rng.choice(n_samples, p=probabilities))
            centres[index] = obs[pick]
        closest = np.minimum(closest, np.sum((obs - centres[index]) ** 2, axis=1))

    labels = np.zeros(n_samples, dtype=np.int64)
    for _ in range(n_iter):
        distances = np.stack(
            [np.sum((obs - centres[state]) ** 2, axis=1) for state in range(n_states)], axis=1
        )
        new_labels = np.asarray(np.argmin(distances, axis=1), dtype=np.int64)
        if np.array_equal(new_labels, labels) and _ > 0:
            labels = new_labels
            break
        labels = new_labels
        for state in range(n_states):
            members = obs[labels == state]
            if members.shape[0] == 0:
                far = int(np.argmax(np.min(distances, axis=1)))
                centres[state] = obs[far]
            else:
                centres[state] = members.mean(axis=0)
    return centres, labels


def _init_parameters(
    obs: FloatArray, n_states: int, *, seed: int, covariance_floor: float
) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
    centres, labels = _kmeans_init(obs, n_states, seed=seed)
    n_features = obs.shape[1]
    global_cov = np.cov(obs, rowvar=False)
    global_cov = np.atleast_2d(np.asarray(global_cov, dtype=np.float64))
    if global_cov.shape != (n_features, n_features):
        global_cov = np.eye(n_features, dtype=np.float64)

    covariances = np.empty((n_states, n_features, n_features), dtype=np.float64)
    start_prob = np.empty(n_states, dtype=np.float64)
    for state in range(n_states):
        members = obs[labels == state]
        if members.shape[0] <= n_features:
            covariances[state] = _regularise(global_cov, covariance_floor)
        else:
            candidate = np.cov(members, rowvar=False)
            covariances[state] = _regularise(
                np.atleast_2d(np.asarray(candidate, dtype=np.float64)), covariance_floor
            )
        start_prob[state] = max(members.shape[0], 1) / obs.shape[0]
    start_prob = start_prob / float(np.sum(start_prob))

    # Empirical transition counts from the k-means label sequence, Laplace
    # smoothed so no transition starts at exactly zero probability.
    transition = np.ones((n_states, n_states), dtype=np.float64)
    for previous, nxt in zip(labels[:-1], labels[1:], strict=False):
        transition[int(previous), int(nxt)] += 1.0
    transition = transition / transition.sum(axis=1, keepdims=True)
    return start_prob, transition, centres, covariances


def forward_backward(
    model: GaussianHMM, observations: npt.ArrayLike
) -> tuple[FloatArray, FloatArray, FloatArray, float]:
    """Run the log-space forward-backward recursions.

    Returns ``(log_alpha, log_beta, posteriors, log_likelihood)`` where
    ``posteriors[t, i] = P(state_t = i | observations)``.
    """
    obs = _as_matrix(observations)
    log_b = _emission_log_prob(model.means, model.covariances, obs)
    log_pi = _safe_log(model.start_prob)
    log_a = _safe_log(model.transition)
    return _forward_backward_arrays(log_pi, log_a, log_b)


def _forward_backward_arrays(
    log_pi: FloatArray, log_a: FloatArray, log_b: FloatArray
) -> tuple[FloatArray, FloatArray, FloatArray, float]:
    n_samples, n_states = log_b.shape
    log_alpha = np.empty((n_samples, n_states), dtype=np.float64)
    log_alpha[0] = log_pi + log_b[0]
    for t in range(1, n_samples):
        stacked = log_alpha[t - 1][:, None] + log_a
        log_alpha[t] = _logsumexp(stacked, axis=0) + log_b[t]

    log_beta = np.zeros((n_samples, n_states), dtype=np.float64)
    for t in range(n_samples - 2, -1, -1):
        stacked = log_a + (log_b[t + 1] + log_beta[t + 1])[None, :]
        log_beta[t] = _logsumexp(stacked, axis=1)

    log_likelihood = float(_logsumexp(log_alpha[-1], axis=None))
    log_gamma = log_alpha + log_beta - log_likelihood
    posteriors = np.exp(log_gamma - _logsumexp(log_gamma, axis=1)[:, None])
    return log_alpha, log_beta, posteriors, log_likelihood


def posterior_states(model: GaussianHMM, observations: npt.ArrayLike) -> FloatArray:
    """Smoothed state posteriors ``P(state_t | all observations)``."""
    _, _, posteriors, _ = forward_backward(model, observations)
    return posteriors


def filtered_posteriors(model: GaussianHMM, observations: npt.ArrayLike) -> FloatArray:
    """Forward-only state posteriors ``P(state_t | observations 1..t)``.

    Unlike :func:`posterior_states` and :func:`viterbi`, which both smooth using
    the *whole* sequence, this uses only data up to and including ``t``. It is
    the decoding to use whenever the label is consumed as a predictor, because a
    smoothed label at ``t`` depends on observations after ``t``.
    """
    log_alpha, _, _, _ = forward_backward(model, observations)
    normaliser = _logsumexp(log_alpha, axis=1)[:, None]
    result: FloatArray = np.exp(log_alpha - normaliser)
    return result


def filtered_states(model: GaussianHMM, observations: npt.ArrayLike) -> IntArray:
    """Most likely state at each ``t`` given only data through ``t``."""
    return np.asarray(
        np.argmax(filtered_posteriors(model, observations), axis=1), dtype=np.int64
    )


def score(model: GaussianHMM, observations: npt.ArrayLike) -> float:
    """Log likelihood of ``observations`` under ``model``."""
    _, _, _, log_likelihood = forward_backward(model, observations)
    return log_likelihood


def viterbi(model: GaussianHMM, observations: npt.ArrayLike) -> IntArray:
    """Most likely state sequence (Viterbi decoding) in log space."""
    obs = _as_matrix(observations)
    log_b = _emission_log_prob(model.means, model.covariances, obs)
    log_pi = _safe_log(model.start_prob)
    log_a = _safe_log(model.transition)

    n_samples, n_states = log_b.shape
    delta = np.empty((n_samples, n_states), dtype=np.float64)
    psi = np.zeros((n_samples, n_states), dtype=np.int64)
    delta[0] = log_pi + log_b[0]
    for t in range(1, n_samples):
        stacked = delta[t - 1][:, None] + log_a
        psi[t] = np.asarray(np.argmax(stacked, axis=0), dtype=np.int64)
        delta[t] = np.max(stacked, axis=0) + log_b[t]

    path = np.zeros(n_samples, dtype=np.int64)
    path[-1] = int(np.argmax(delta[-1]))
    for t in range(n_samples - 2, -1, -1):
        path[t] = psi[t + 1, path[t + 1]]
    return path


def fit_gaussian_hmm(
    observations: npt.ArrayLike,
    *,
    n_states: int = 3,
    n_iter: int = 200,
    tol: float = 1e-4,
    seed: int = _DEFAULT_SEED,
    covariance_floor: float = 1e-6,
    n_init: int = 4,
) -> GaussianHMM:
    """Fit a full-covariance Gaussian HMM by Baum-Welch EM.

    Parameters
    ----------
    observations:
        ``(n_samples, n_features)`` finite observation matrix.
    n_states:
        Number of hidden states (3 in the reference paper).
    n_iter:
        Maximum EM iterations.
    tol:
        Convergence tolerance on the *per-sample* improvement in log likelihood.
    seed:
        Seeds the deterministic k-means++ initialisation; the same data and seed
        always produce the same fit.
    covariance_floor:
        Relative ridge added to every covariance update.
    n_init:
        Number of independent restarts. EM only finds a local optimum, so the
        fit is repeated from ``n_init`` deterministically derived seeds and the
        run with the highest log likelihood is returned. Still fully
        reproducible: the seeds are ``seed, seed + 1, ...``.

    Notes
    -----
    EM is monotone in log likelihood; the loop also stops early if an update
    ever *decreases* the likelihood by more than the tolerance (a symptom of
    numerical trouble), returning the last good parameters.
    """
    if n_init < 1:
        raise HMMError("n_init must be >= 1")
    best: GaussianHMM | None = None
    for offset in range(n_init):
        candidate = _fit_once(
            observations,
            n_states=n_states,
            n_iter=n_iter,
            tol=tol,
            seed=seed + offset,
            covariance_floor=covariance_floor,
        )
        if best is None or candidate.log_likelihood > best.log_likelihood:
            best = candidate
    assert best is not None  # noqa: S101 - n_init >= 1 guarantees a fit
    return best


def _fit_once(
    observations: npt.ArrayLike,
    *,
    n_states: int,
    n_iter: int,
    tol: float,
    seed: int,
    covariance_floor: float,
) -> GaussianHMM:
    obs = _as_matrix(observations)
    if n_states < 1:
        raise HMMError("n_states must be >= 1")
    if obs.shape[0] < n_states * 2:
        raise HMMError(f"need at least {n_states * 2} samples to fit {n_states} states")

    start_prob, transition, means, covariances = _init_parameters(
        obs, n_states, seed=seed, covariance_floor=covariance_floor
    )
    n_samples, n_features = obs.shape
    trace: list[float] = []
    previous = -np.inf
    converged = False
    iterations = 0

    for iteration in range(1, n_iter + 1):
        iterations = iteration
        log_b = _emission_log_prob(means, covariances, obs)
        log_pi = _safe_log(start_prob)
        log_a = _safe_log(transition)
        log_alpha, log_beta, posteriors, log_likelihood = _forward_backward_arrays(
            log_pi, log_a, log_b
        )
        trace.append(log_likelihood)

        improvement = (log_likelihood - previous) / max(n_samples, 1)
        if np.isfinite(previous) and improvement < -abs(tol):
            break
        if np.isfinite(previous) and abs(improvement) < tol:
            converged = True
            previous = log_likelihood
            break
        previous = log_likelihood

        if n_samples > 1:
            log_xi = (
                log_alpha[:-1, :, None]
                + log_a[None, :, :]
                + (log_b[1:] + log_beta[1:])[:, None, :]
                - log_likelihood
            )
            normaliser = _logsumexp(log_xi.reshape(n_samples - 1, -1), axis=1)
            xi = np.exp(log_xi - normaliser[:, None, None])
            numerator = xi.sum(axis=0)
        else:
            numerator = np.ones((n_states, n_states), dtype=np.float64)

        denominator = numerator.sum(axis=1, keepdims=True)
        safe = denominator > 0.0
        new_transition = np.where(
            safe, numerator / np.where(safe, denominator, 1.0), 1.0 / n_states
        )

        weights = posteriors.sum(axis=0)
        new_means = np.empty_like(means)
        new_covs = np.empty_like(covariances)
        for state in range(n_states):
            weight = float(weights[state])
            if weight <= 1e-12:
                # Degenerate state: keep the previous emission rather than
                # dividing by ~0, which would blow the covariance up.
                new_means[state] = means[state]
                new_covs[state] = _regularise(covariances[state], covariance_floor)
                continue
            gamma = posteriors[:, state][:, None]
            mean = (gamma * obs).sum(axis=0) / weight
            delta = obs - mean[None, :]
            cov = (gamma * delta).T @ delta / weight
            new_means[state] = mean
            new_covs[state] = _regularise(
                np.asarray(cov, dtype=np.float64).reshape(n_features, n_features),
                covariance_floor,
            )

        new_start = posteriors[0].copy()
        total_start = float(new_start.sum())
        start_prob = (
            new_start / total_start
            if total_start > 0
            else np.full(n_states, 1.0 / n_states)
        )
        transition = new_transition
        means = new_means
        covariances = new_covs

    return GaussianHMM(
        start_prob=start_prob,
        transition=transition,
        means=means,
        covariances=covariances,
        log_likelihood=float(previous if np.isfinite(previous) else (trace[-1] if trace else 0.0)),
        n_iter_run=iterations,
        converged=converged,
        log_likelihood_trace=tuple(float(value) for value in trace),
    )


def expected_durations(transition: npt.ArrayLike) -> FloatArray:
    """Expected sojourn time per state, ``1 / (1 - A_ii)`` in days.

    A perfectly absorbing state (``A_ii == 1``) reports ``inf``.
    """
    matrix = np.asarray(transition, dtype=np.float64)
    diagonal = np.clip(np.diag(matrix), 0.0, 1.0)
    with np.errstate(divide="ignore"):
        durations = 1.0 / (1.0 - diagonal)
    return np.asarray(durations, dtype=np.float64)


def stationary_distribution(transition: npt.ArrayLike) -> FloatArray:
    """Long-run occupancy implied by the transition matrix.

    Computed as the left eigenvector of ``A`` for eigenvalue 1; falls back to a
    power iteration if the eigen decomposition is not usable.
    """
    matrix = np.asarray(transition, dtype=np.float64)
    n_states = matrix.shape[0]
    try:
        values, vectors = np.linalg.eig(matrix.T)
        index = int(np.argmin(np.abs(values - 1.0)))
        vector = np.real(vectors[:, index])
        total = float(np.sum(vector))
        if np.isfinite(total) and abs(total) > 1e-12:
            candidate = vector / total
            if np.all(candidate >= -1e-9):
                return np.clip(np.asarray(candidate, dtype=np.float64), 0.0, None)
    except np.linalg.LinAlgError:  # pragma: no cover - numpy rarely fails here
        pass
    distribution = np.full(n_states, 1.0 / n_states, dtype=np.float64)
    for _ in range(2000):
        nxt = distribution @ matrix
        total = float(np.sum(nxt))
        if total <= 0:
            break
        nxt = nxt / total
        if float(np.max(np.abs(nxt - distribution))) < 1e-12:
            distribution = nxt
            break
        distribution = nxt
    return distribution
