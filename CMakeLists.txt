cmake_minimum_required(VERSION 3.16)
project(harmony_pc_operation_recognition LANGUAGES CXX)

include(CTest)
add_subdirectory(native)

# Reuse the frozen WPS C collector on Linux; do not cross-build it for macOS
# or silently substitute a different smaps/segment transform.
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    enable_language(C)
    add_subdirectory(collector/linux)
endif()
