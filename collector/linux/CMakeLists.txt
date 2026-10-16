add_executable(linux_vma_collect ../device/collect.c)
set_target_properties(linux_vma_collect PROPERTIES C_STANDARD 11 C_STANDARD_REQUIRED YES)
target_compile_options(linux_vma_collect PRIVATE -Wall -Wextra -Wpedantic)
