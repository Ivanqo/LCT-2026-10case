# Reference data bundled with the code

Public files from the organizer's participant package (`01_participant_package/…/02_ФОРМАТ_ДАННЫХ_И_ПРИМЕРЫ/data/`),
copied unchanged so the service and `python -m app.cli` run without any mounted organizer folder:

| File | SHA-256 | Used for |
|---|---|---|
| `case_data/parameter_catalog_132.jsonl` | `c6b73dffbea6bb366fc50f08392522c390bdf7b19420051802bd0bcbd32b4e4f` | the 132-parameter matrix (codes M-001…M-132 come from `app/domain/matrix_v11_codes.json`) |
| `case_data/submission_schema.json` | `75c58bef6b580528e7e4af8e4fdcdf5a5d40599b8966dae90df38b3a5f04a8d7` | validation of every result JSON |

No gold labels, answers, annotations or documents are stored here. The folder is named `case_data` because
`official_dataset.find_dataset_paths()` recognizes a dataset root by that name (`CASE10_DATASET_ROOT`).
