/* A real shared-host link using the public C facade and its transitive libs.
 * No registration, focus authority, collection or UI execution here. */
#include "harmony_ops/embedded.h"
hop_status example_existing_service_start(const char *package_dir,const char *output_dir,
                                         hop_embedded **out,hop_error *error) {
    return hop_embedded_start(package_dir,output_dir,"wps",out,error);
}
hop_status example_existing_service_trigger(hop_embedded *service,hop_error *error) {
    return hop_embedded_trigger(service,error);
}
hop_status example_existing_service_stop(hop_embedded **service,hop_error *error) {
    return hop_embedded_stop(service,error);
}
