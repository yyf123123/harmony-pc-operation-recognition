"""Shared collection primitives used by application-level VMA collectors."""

from .collection_contract import CollectionContract, cross_app_hdc_contract, frozen_wps_contract

__all__ = ["CollectionContract", "cross_app_hdc_contract", "frozen_wps_contract"]
