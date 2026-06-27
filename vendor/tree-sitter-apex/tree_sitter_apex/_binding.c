#include <Python.h>

typedef struct TSLanguage TSLanguage;
const TSLanguage *tree_sitter_apex(void);

static PyObject *_language(PyObject *self, PyObject *args) {
    (void)self; (void)args;
    return PyCapsule_New((void *)tree_sitter_apex(), "tree_sitter.Language", NULL);
}

static PyMethodDef methods[] = {
    {"language", _language, METH_NOARGS,
     "Get the tree-sitter language capsule for Apex."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module = {
    .m_base = PyModuleDef_HEAD_INIT,
    .m_name = "_binding",
    .m_doc = NULL,
    .m_size = -1,
    .m_methods = methods,
};

PyMODINIT_FUNC PyInit__binding(void) {
    return PyModule_Create(&module);
}
