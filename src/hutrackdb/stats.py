"""Small, dependency-free statistics used by the analysis scripts.

numpy and the standard library only, so the base install needs neither scipy
nor statsmodels. The coverage level is always passed in by the caller and comes
from the ``confidence_level`` calibration in config/pipeline.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import NormalDist

import numpy as np


def z_for(level: float) -> float:
    """Two-sided standard-normal critical value for a coverage ``level``."""
    return NormalDist().inv_cdf(1 - (1 - level) / 2)


def wilson_interval(successes: int, trials: int, level: float) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (Wilson 1927).

    Returns (nan, nan) for zero trials rather than inventing an interval.
    """
    if trials == 0:
        return float("nan"), float("nan")
    z = z_for(level)
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * np.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class LogisticFit:
    intercept: float
    slope: float
    covariance: np.ndarray
    n: int
    events: int

    def predict(self, x: np.ndarray, level: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Fitted probability and a Wald band, computed on the logit scale."""
        design = np.column_stack([np.ones_like(x), x])
        eta = design @ np.array([self.intercept, self.slope])
        se = np.sqrt(np.einsum("ij,jk,ik->i", design, self.covariance, design))
        z = z_for(level)
        expit = lambda v: 1 / (1 + np.exp(-v))  # noqa: E731
        return expit(eta), expit(eta - z * se), expit(eta + z * se)

    def slope_interval(self, level: float) -> tuple[float, float]:
        se = float(np.sqrt(self.covariance[1, 1]))
        z = z_for(level)
        return self.slope - z * se, self.slope + z * se

    def slope_p_value(self) -> float:
        """Two-sided Wald p-value for slope = 0."""
        z = abs(self.slope) / float(np.sqrt(self.covariance[1, 1]))
        return 2 * (1 - NormalDist().cdf(z))


def _newton(design: np.ndarray, y: np.ndarray, family: str,
            beta: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Newton-Raphson for a canonical-link GLM ("logit" or "poisson").

    Stops when a step no longer increases the log-likelihood -- the point at
    which floating point, not the model, limits progress. That rule has no
    tolerance to choose. Raises under complete separation, detected as fitted
    means saturating at their bounds.

    Returns (coefficients, covariance = inverse Fisher information).
    """
    def stats(b):
        eta = design @ b
        if family == "logit":
            mean = 1 / (1 + np.exp(-eta))
            weight = mean * (1 - mean)
            loglik = float(np.sum(y * eta - np.logaddexp(0, eta)))
        else:
            mean = np.exp(eta)
            weight = mean
            loglik = float(np.sum(y * eta - mean))
        return mean, weight, loglik

    mean, weight, loglik = stats(beta)
    while True:
        hessian = design.T @ (design * weight[:, None])
        try:
            candidate = beta + np.linalg.solve(hessian, design.T @ (y - mean))
        except np.linalg.LinAlgError:
            raise RuntimeError(f"{family} fit is separated or collinear: the "
                               f"information matrix became singular") from None
        c_mean, c_weight, c_loglik = stats(candidate)
        if not c_loglik > loglik:
            break
        beta, mean, weight, loglik = candidate, c_mean, c_weight, c_loglik
    # Separation drives estimates toward infinity until floating point
    # saturates the fitted means at their bounds; that, not an iteration
    # count, is the signal.
    eta = design @ beta
    if family == "logit" and np.all((eta > 0) == (y == 1)) and np.all(eta != 0):
        # A finite linear predictor that classifies every observation
        # correctly is the definition of separation: no finite MLE exists.
        raise RuntimeError("logit fit is separated: the covariates perfectly "
                           "predict the outcome, so estimates are infinite")
    if np.any(weight == 0):
        raise RuntimeError(f"{family} fit is separated: some fitted means hit "
                           f"their bound, so estimates are infinite")
    covariance = np.linalg.inv(design.T @ (design * weight[:, None]))
    return beta, covariance


def fit_logistic(x: np.ndarray, y: np.ndarray) -> LogisticFit:
    """Maximum-likelihood logit P(y=1) = expit(a + b x). See :func:`_newton`."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    design = np.column_stack([np.ones_like(x), x])
    beta, covariance = _newton(design, y, "logit", np.zeros(2))
    return LogisticFit(intercept=float(beta[0]), slope=float(beta[1]),
                       covariance=covariance, n=len(x), events=int(y.sum()))


@dataclass(frozen=True)
class PoissonFit:
    coef: np.ndarray
    covariance: np.ndarray
    names: list[str]

    def term(self, name: str) -> tuple[float, float]:
        """Coefficient and standard error for one named column."""
        i = self.names.index(name)
        return float(self.coef[i]), float(np.sqrt(self.covariance[i, i]))


def fit_poisson(y: np.ndarray, design: np.ndarray, names: list[str]) -> PoissonFit:
    """Maximum-likelihood Poisson log-linear model by Newton-Raphson.

    ``design`` must already carry whatever intercept / fixed-effect columns
    the model needs. Stopping rule: see :func:`_newton`.
    """
    y = np.asarray(y, dtype=float)
    design = np.asarray(design, dtype=float)
    # Start each fixed effect at the log of its group mean, slope terms at 0,
    # which keeps the first Newton step well inside the likelihood's basin.
    beta = np.zeros(design.shape[1])
    for j in range(design.shape[1]):
        column = design[:, j]
        if set(np.unique(column)) <= {0.0, 1.0} and y[column == 1].sum() > 0:
            beta[j] = np.log(y[column == 1].mean())
    beta, covariance = _newton(design, y, "poisson", beta)
    return PoissonFit(coef=beta, covariance=covariance, names=list(names))


def two_sided_p(z: float) -> float:
    return 2 * (1 - NormalDist().cdf(abs(z)))


def fit_logit(y: np.ndarray, design: np.ndarray, names: list[str]) -> PoissonFit:
    """Multi-covariate maximum-likelihood logit by Newton-Raphson.

    ``design`` carries its own intercept column. Returns the same coefficient
    container as :func:`fit_poisson` (``term(name)`` -> coefficient, SE).
    Raises under complete separation rather than returning infinite estimates.
    """
    y = np.asarray(y, dtype=float)
    design = np.asarray(design, dtype=float)
    beta, covariance = _newton(design, y, "logit", np.zeros(design.shape[1]))
    return PoissonFit(coef=beta, covariance=covariance, names=list(names))
