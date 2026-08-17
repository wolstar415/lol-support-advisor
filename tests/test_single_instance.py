from __future__ import annotations

import os
import unittest
from uuid import uuid4

from lol_support_advisor.single_instance import SingleInstanceLock


@unittest.skipUnless(os.name == "nt", "Windows named mutex test")
class SingleInstanceTests(unittest.TestCase):
    def test_second_lock_is_rejected_until_first_process_lock_is_released(self) -> None:
        name = rf"Local\LOL-Pick-Advisor-Test-{uuid4()}"
        first = SingleInstanceLock(name)
        second = SingleInstanceLock(name)
        third = SingleInstanceLock(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(third.acquire())
        finally:
            first.release()
            second.release()
            third.release()


if __name__ == "__main__":
    unittest.main()
