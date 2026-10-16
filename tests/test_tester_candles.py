from __future__ import annotations

from io import BytesIO

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from mrs3.tester_candles import HEADER, PARQUET_SCHEMA, CandleDayError, encode_day, read_day

COLUMNS = {
    "timestamp": [1_789_603_200_000, 1_789_603_260_000],
    "open": [332.96, 332.86],
    "high": [332.96, 332.9],
    "low": [332.96, 332.8],
    "close": [332.96, 332.86],
    "volume": [0.35, 1.24],
    "buy_volume": [0.15, 0.94],
    "sell_volume": [0.2, 0.3],
    "trades": [4, 4],
}


def _write(schema: pa.Schema, *, compression: str = "zstd", store_schema: bool = False, metadata=None) -> bytes:
    sink = BytesIO()
    table = pa.Table.from_arrays([pa.array(COLUMNS[name], type=field.type) for name, field in zip(HEADER, schema)], schema=schema)
    with pq.ParquetWriter(sink, schema, compression=compression, store_schema=store_schema) as writer:
        writer.write_table(table)
        if metadata:
            writer.add_key_value_metadata(metadata)
    return sink.getvalue()


def test_encoded_day_matches_the_tester_file_format():
    # Reference: a day file written by the tester (Parquet.Net) — REQUIRED columns, ZSTD, one key-value entry.
    parquet = pq.ParquetFile(BytesIO(encode_day(COLUMNS)))
    assert parquet.schema_arrow.names == list(HEADER)
    assert [field.type for field in parquet.schema_arrow] == [pa.int64()] + [pa.float64()] * 7 + [pa.int32()]
    assert not any(field.nullable for field in parquet.schema_arrow)
    assert parquet.metadata.metadata == {b"hb.schema": b"candles_1m/1"}
    group = parquet.metadata.row_group(0)
    assert {group.column(index).compression for index in range(group.num_columns)} == {"ZSTD"}
    assert read_day(encode_day(COLUMNS)).to_pydict() == COLUMNS


def test_read_day_rejects_files_the_tester_would_not_write():
    nullable = pa.schema([field.with_nullable(True) for field in PARQUET_SCHEMA])
    key_value = {"hb.schema": "candles_1m/1"}
    with pytest.raises(CandleDayError, match="REQUIRED"):
        read_day(_write(nullable, metadata=key_value))
    with pytest.raises(CandleDayError, match="ZSTD"):
        read_day(_write(PARQUET_SCHEMA, compression="snappy", metadata=key_value))
    with pytest.raises(CandleDayError, match="metadata"):
        read_day(_write(PARQUET_SCHEMA))  # no hb.schema entry
    with pytest.raises(CandleDayError, match="metadata"):
        read_day(_write(PARQUET_SCHEMA, store_schema=True, metadata=key_value))  # extra ARROW:schema entry
    renamed = pa.schema([PARQUET_SCHEMA.field(0).with_name("time"), *list(PARQUET_SCHEMA)[1:]])
    with pytest.raises(CandleDayError, match="columns"):
        read_day(_write(renamed, metadata=key_value))
    with pytest.raises(CandleDayError, match="parquet"):
        read_day(b"timestamp,open\n")


def test_encode_day_rejects_missing_values():
    with pytest.raises(CandleDayError):
        encode_day({**COLUMNS, "volume": [0.35, None]})
