import unittest
from unittest.mock import patch

from test_cbs_route_preparation import (
    HOPS,
    StrictCBSResolver,
    payload,
    usecase,
)
from cbs_route_preparation import (
    CBSRoutePreparationError,
    prepare_cbs_routes,
)

# These tests use the existing route fixtures and mock the routing-mode flag to test both code paths. 
# They do not run the full optimizer or Docker containers.
class TestCBSVariants(unittest.TestCase):

    def setUp(self):
        self.optimizer = StrictCBSResolver()

    @patch("cbs_route_preparation.ROUTING_ENABLED", False)
    def test_zhao_cbs_accepts_fixed_routes(self):
        request = payload(routing="fixed", hops=HOPS)
        result = prepare_cbs_routes(
            request, usecase(request), self.optimizer
        )
        self.assertEqual(result, {})

    @patch("cbs_route_preparation.ROUTING_ENABLED", False)
    def test_zhao_cbs_rejects_missing_routes(self):
        request = payload(routing="fixed")
        with self.assertRaises(CBSRoutePreparationError):
            prepare_cbs_routes(
                request, usecase(request), self.optimizer
            )

    @patch("cbs_route_preparation.ROUTING_ENABLED", False)
    def test_zhao_cbs_rejects_routing_optimization(self):
        request = payload(routing="optimize")
        with self.assertRaises(CBSRoutePreparationError):
            prepare_cbs_routes(
                request, usecase(request), self.optimizer
            )

    @patch("cbs_route_preparation.ROUTING_ENABLED", True)
    def test_zhao_cbs_routing_generates_routes(self):
        request = payload(routing="optimize")
        result = prepare_cbs_routes(
            request, usecase(request), self.optimizer
        )
        self.assertEqual(result["f1"], HOPS)

    @patch("cbs_route_preparation.ROUTING_ENABLED", True)
    def test_zhao_cbs_routing_accepts_fixed_routes(self):
        request = payload(routing="fixed", hops=HOPS)
        result = prepare_cbs_routes(
            request, usecase(request), self.optimizer
        )
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()