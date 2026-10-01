// Resident input boundary around unchanged Phil simulate(); no model formulas.
#define main phil_original_main
#include "ref_model_v2.cpp"
#undef main

struct Dataset {
    std::vector<Candle> market;
    std::vector<double> oracle;
};
extern "C" void *load_points(const double *points, size_t count) {
    auto *data = new Dataset;
    data->market.reserve(count);
    data->oracle.reserve(count);
    for (size_t i = 0; i < count; ++i) {
        const double *p = points + i * 7;
        data->market.push_back({p[0], p[1], p[2], p[3], p[4]});
        data->oracle.push_back(p[6]);
    }
    return data;
}
extern "C" void free_points(void *pointer) { delete static_cast<Dataset *>(pointer); }
extern "C" void replay(void *pointer, const double *records, size_t count, double *output) {
    auto &data = *static_cast<Dataset *>(pointer);
    for (size_t i = 0; i < count; ++i) {
        const double *r = records + i * 8;
        output[i] = simulate(data.market, data.oracle, r[0], r[1], size_t(r[2]), size_t(r[3]-r[2]), int(r[4]), r[5], r[6]);
    }
}
