# inkeep

Merge-ready rate 0.41 ± 0.01 over 3 rep(s) (Inkeep historical 0.65).

| metric                                    | value |
| ----------------------------------------- | ----- |
| merge-ready rate                          | 0.41  |
| drafts                                    | 21/40 |
| mean page overlap                         | 0.52  |
| mean edit size (lines)                    | 28.1  |
| drafts that do not apply at the docs base | 2     |
| Vale errors on added lines                | 16    |
| Vale warnings on added lines              | 25    |

### carbon-unit

| metric                                    | value |
| ----------------------------------------- | ----- |
| merge-ready rate                          | 0.02  |
| drafts                                    | 3/21  |
| mean page overlap                         | 0.14  |
| mean edit size (lines)                    | 51.9  |
| drafts that do not apply at the docs base | 1     |
| Vale errors on added lines                | 15    |
| Vale warnings on added lines              | 4     |

### edited-inkeep

| metric                                    | value |
| ----------------------------------------- | ----- |
| merge-ready rate                          | 0.33  |
| drafts                                    | 2/3   |
| mean page overlap                         | 0.67  |
| mean edit size (lines)                    | 12.0  |
| drafts that do not apply at the docs base | 1     |
| Vale errors on added lines                | 0     |
| Vale warnings on added lines              | 2     |

### unedited-inkeep

| metric                                    | value |
| ----------------------------------------- | ----- |
| merge-ready rate                          | 0.94  |
| drafts                                    | 16/16 |
| mean page overlap                         | 1.00  |
| mean edit size (lines)                    | 0.0   |
| drafts that do not apply at the docs base | 0     |
| Vale errors on added lines                | 1     |
| Vale warnings on added lines              | 19    |

## Cases

| case           | kind            | ready | wrong | missing | draft | pages | edit | vale errors    | vale warnings  |
| -------------- | --------------- | ----- | ----- | ------- | ----- | ----- | ---- | -------------- | -------------- |
| posthog-102724 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 367  | -              | -              |
| posthog-103761 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 4    | -              | -              |
| posthog-104223 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 46   | -              | -              |
| posthog-105555 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 25   | -              | -              |
| posthog-105864 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 15   | -              | -              |
| posthog-106324 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 10   | -              | -              |
| posthog-107559 | carbon-unit     | 1/3   | 0.0   | 0.7     | yes   | 1.00  | 18   | 0              | 0              |
| posthog-108384 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 2    | -              | -              |
| posthog-110078 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 8    | -              | -              |
| posthog-110396 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 9    | -              | -              |
| posthog-112419 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 173  | -              | -              |
| posthog-112507 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 0              |
| posthog-112758 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 0              |
| posthog-113080 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 12   | -              | -              |
| posthog-114018 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 0              |
| posthog-114048 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 19   | -              | -              |
| posthog-115136 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 0              |
| posthog-116004 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 14   | -              | -              |
| posthog-116576 | carbon-unit     | 0/3   | 1.0   | 8.3     | yes   | 0.86  | 121  | 15             | 4              |
| posthog-116939 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 0              |
| posthog-117001 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 56   | -              | -              |
| posthog-117455 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 0              |
| posthog-117706 | edited-inkeep   | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 2    | 0              | 2              |
| posthog-118111 | carbon-unit     | 0/3   | -     | -       | yes   | 1.00  | 24   | does not apply | does not apply |
| posthog-118821 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 1              |
| posthog-118855 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 7              |
| posthog-119686 | unedited-inkeep | 0/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 1              | 1              |
| posthog-120497 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 1              |
| posthog-120738 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 0              |
| posthog-121042 | edited-inkeep   | 0/3   | -     | -       | no    | 0.00  | 13   | -              | -              |
| posthog-121343 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 1              |
| posthog-121616 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 0              |
| posthog-122171 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 4    | -              | -              |
| posthog-122298 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 1              |
| posthog-122853 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 2    | -              | -              |
| posthog-124060 | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 5    | -              | -              |
| posthog-124858 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 0              |
| posthog-125427 | edited-inkeep   | 0/3   | -     | -       | yes   | 1.00  | 21   | does not apply | does not apply |
| posthog-125438 | unedited-inkeep | 3/3   | 0.0   | 0.0     | yes   | 1.00  | 0    | 0              | 7              |
| posthog-97528  | carbon-unit     | 0/3   | -     | -       | no    | 0.00  | 156  | -              | -              |
