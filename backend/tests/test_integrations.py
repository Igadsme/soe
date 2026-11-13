import pytest
from app.integrations import FixtureProvider


@pytest.mark.asyncio
async def test_fixture_provider_is_explicitly_marked():
    result = await FixtureProvider("youtube").search("gaming")
    assert result and all(item.metadata["fixture"] for item in result)
