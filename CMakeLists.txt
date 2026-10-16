cmake_minimum_required(VERSION 3.16)
project(harmony_pc_operation_recognition LANGUAGES C CXX)

include(CTest)
find_package(Threads REQUIRED)
add_subdirectory(collector)
add_subdirectory(native)
