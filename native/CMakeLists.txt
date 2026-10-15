add_library(harmony_operation_recognition STATIC
    src/recognition.cpp
    src/feature.cpp
    src/pipeline.cpp)
target_include_directories(harmony_operation_recognition PUBLIC include)
target_compile_features(harmony_operation_recognition PUBLIC cxx_std_17)
if(MSVC)
    target_compile_options(harmony_operation_recognition PRIVATE /W4)
else()
    target_compile_options(harmony_operation_recognition PRIVATE -Wall -Wextra -Wpedantic)
endif()

add_executable(operation_predict examples/offline_predict.cpp)
target_link_libraries(operation_predict PRIVATE harmony_operation_recognition)

add_executable(operation_feature examples/feature_vector.cpp)
target_link_libraries(operation_feature PRIVATE harmony_operation_recognition)

# Compile the service-side interface skeleton without registering a service.
add_library(operation_adapter_template OBJECT examples/service_adapter_template.cpp)
target_link_libraries(operation_adapter_template PRIVATE harmony_operation_recognition)

if(BUILD_TESTING)
    add_executable(operation_pipeline_test tests/pipeline_test.cpp)
    target_link_libraries(operation_pipeline_test PRIVATE harmony_operation_recognition)
    add_test(NAME operation_pipeline_test
             COMMAND operation_pipeline_test ${PROJECT_SOURCE_DIR}/models)
endif()
