__all__ = ["build_pdf", "build_csv", "build_csv_bytes"]


def __getattr__(name):
    # Import lazily so that pulling in report submodules (filters, azure_rg, …)
    # does not require WeasyPrint's native libraries unless a PDF is built.
    if name == "build_pdf":
        from .pdf_builder import build_pdf

        return build_pdf
    if name in ("build_csv", "build_csv_bytes"):
        from . import csv_builder

        return getattr(csv_builder, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
