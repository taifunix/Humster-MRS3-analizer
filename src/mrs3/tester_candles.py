"""The tester's minute-candle day file (``*_1m.parquet``): one format for every exchange folder.

It is what the tester writes itself and what its built-in converter
(``hb_c.exe --convert-data``) produces from a ``*_1m.csv`` day: REQUIRED
columns, ZSTD, and exactly one key-value entry (no Arrow schema blob).
DuckDB cannot write REQUIRED columns, so pyarrow is used.
"""

from __future__ import annotations

from io import BytesIO
from typing import Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq


HEADER = ("timestamp", "open", "high", "low", "close", "volume", "buy_volume", "sell_volume", "trades")
PARQUET_SCHEMA = pa.schema(
    [pa.field("timestamp", pa.int64(), nullable=False)]
    + [pa.field(name, pa.float64(), nullable=False) for name in HEADER[1:8]]
    + [pa.field("trades", pa.int32(), nullable=False)]
)
PARQUET_KEY_VALUE = {"hb.schema": "candles_1m/1"}
_KEY_VALUE_BYTES = {key.encode(): value.encode() for key, value in PARQUET_KEY_VALUE.items()}


class CandleDayError(ValueError):
    pass


def encode_day(columns: Mapping[str, Sequence]) -> bytes:
    """Encode one day of minute candles, given column -> values, in the tester's parquet format."""
    try:
        table = pa.Table.from_pydict({name: columns[name] for name in HEADER}, schema=PARQUET_SCHEMA)
    except (KeyError, pa.ArrowException, TypeError, ValueError) as error:
        raise CandleDayError(f"cannot encode the day: {error}") from error
    if any(column.null_count for column in table.columns):  # pyarrow does not enforce REQUIRED on input
        raise CandleDayError("cannot encode the day: missing values")
    sink = BytesIO()
    with pq.ParquetWriter(sink, PARQUET_SCHEMA, compression="zstd", store_schema=False) as writer:
        writer.write_table(table)
        writer.add_key_value_metadata(PARQUET_KEY_VALUE)
    return sink.getvalue()


def read_day(data: bytes) -> pa.Table:
    """Read a day file and check the file format (not the rows): columns, REQUIRED, ZSTD, metadata."""
    try:
        parquet = pq.ParquetFile(pa.BufferReader(data))
        table = parquet.read()
    except (pa.ArrowException, OSError) as error:
        raise CandleDayError("not a readable parquet file") from error
    schema = parquet.schema_arrow
    if tuple(schema.names) != HEADER or [field.type for field in schema] != [field.type for field in PARQUET_SCHEMA]:
        raise CandleDayError("columns or column types differ from the tester format")
    if any(field.nullable for field in schema):
        raise CandleDayError("every column must be REQUIRED")
    if any(column.null_count for column in table.columns):
        raise CandleDayError("missing values")
    metadata = parquet.metadata
    for group in range(metadata.num_row_groups):
        row_group = metadata.row_group(group)
        if any(row_group.column(index).compression != "ZSTD" for index in range(row_group.num_columns)):
            raise CandleDayError("every column must be ZSTD-compressed")
    if metadata.metadata != _KEY_VALUE_BYTES:
        raise CandleDayError("key-value metadata must be exactly hb.schema=candles_1m/1")
    return table


__all__ = ["HEADER", "PARQUET_KEY_VALUE", "PARQUET_SCHEMA", "CandleDayError", "encode_day", "read_day"]
