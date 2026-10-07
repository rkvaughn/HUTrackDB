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


def fit_logistic(x: np.ndarray, y: np.ndarray) -> LogisticFit:
    """Maximum-likelihood logit P(y=1) = expit(a + b x) by Newton-Raphson.

    Iterates until the parameter vector stops changing to machine precision,
    so there is no tolerance to choose. Raises if it does not settle, which
    happens only under complete separation.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    design = np.column_stack([np.ones_like(x), x])
    beta = np.zeros(2)
    for _ in range(len(x)):
        prob = 1 / (1 + np.exp(-(design @ beta)))
        weight = prob * (1 - prob)
        hessian = design.T @ (design * weight[:, None])
        step = np.linalg.solve(hessian, design.T @ (y - prob))
        new = beta + step
        if np.array_equal(new, beta) or np.allclose(new, beta, rtol=np.finfo(float).eps, atol=0):
            break
        beta = new
    else:
        raise RuntimeError("logistic fit did not converge (separated data?)")
    prob = 1 / (1 + np.exp(-(design @ beta)))
    hessian = design.T @ (design * (prob * (1 - prob))[:, None])
    return LogisticFit(intercept=float(beta[0]), slope=float(beta[1]),
                       covariance=np.linalg.inv(hessian),
                       n=len(x), events=int(y.sum()))


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
    the model needs. Same stopping rule as :func:`fit_logistic`: iterate
    until the parameters stop changing to machine precision.
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
    for _ in range(len(y)):
        mu = np.exp(design @ beta)
        hessian = design.T @ (design * mu[:, None])
        new = beta + np.linalg.solve(hessian, design.T @ (y - mu))
        if np.allclose(new, beta, rtol=np.finfo(float).eps, atol=0):
            beta = new
            break
        beta = new
    else:
        raise RuntimeError("Poisson fit did not converge")
    mu = np.exp(design @ beta)
    covariance = np.linalg.inv(design.T @ (design * mu[:, None]))
    return PoissonFit(coef=beta, covariance=covariance, names=list(names))


def two_sided_p(z: float) -> float:
    return 2 * (1 - NormalDist().cdf(abs(z)))
