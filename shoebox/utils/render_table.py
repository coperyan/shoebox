import pandas as pd
from rich.table import Table


def render_table(df_slice: pd.DataFrame, *, title: str = "") -> Table:
    table = Table(title=title, show_lines=True, expand=True)

    for col in df_slice.columns:
        table.add_column(str(col), overflow="fold", no_wrap=False)

    for _, row in df_slice.iterrows():
        table.add_row(*[str(v) for v in row.values])

    return table
