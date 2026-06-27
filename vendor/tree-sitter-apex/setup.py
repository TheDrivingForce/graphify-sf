from setuptools import setup, Extension

setup(
    ext_modules=[
        Extension(
            name="tree_sitter_apex._binding",
            sources=[
                "tree_sitter_apex/_binding.c",
                "tree_sitter_apex/_src/parser.c",
            ],
            include_dirs=["tree_sitter_apex/_src"],
            extra_compile_args=["-std=c11"],
            define_macros=[("PY_SSIZE_T_CLEAN", None)],
        )
    ]
)
