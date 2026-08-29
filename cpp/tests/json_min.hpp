// Minimal JSON parser for the golden test suite only (reads the small
// tests/golden/*.json files). Header-only, no dependencies, throws
// std::runtime_error on malformed input. Not used by the library.

#pragma once

#include <cstdint>
#include <cstdlib>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace iap_test {

struct Json {
    enum class Type { Null, Bool, Num, Str, Arr, Obj };

    Type type = Type::Null;
    bool boolean = false;
    // Numbers: magnitude + sign (exact for u64-range ints) and double form.
    std::uint64_t mag = 0;
    bool neg = false;
    bool is_int = false;
    double dbl = 0.0;
    std::string str;
    std::vector<Json> arr;
    std::map<std::string, Json> obj;

    std::int64_t i64() const {
        require(Type::Num, "int");
        if (neg) return -static_cast<std::int64_t>(mag);
        return static_cast<std::int64_t>(mag);
    }
    std::uint64_t u64() const {
        require(Type::Num, "uint");
        if (neg) throw std::runtime_error("negative value for u64");
        return mag;
    }
    double num() const {
        require(Type::Num, "number");
        return dbl;
    }
    const std::string& s() const {
        require(Type::Str, "string");
        return str;
    }
    const std::vector<Json>& a() const {
        require(Type::Arr, "array");
        return arr;
    }
    const Json& operator[](const std::string& key) const {
        require(Type::Obj, "object");
        auto it = obj.find(key);
        if (it == obj.end()) {
            throw std::runtime_error("missing JSON key: " + key);
        }
        return it->second;
    }
    bool has(const std::string& key) const {
        return type == Type::Obj && obj.count(key) != 0;
    }

private:
    void require(Type t, const char* what) const {
        if (type != t) {
            throw std::runtime_error(std::string("JSON value is not a ") +
                                     what);
        }
    }
};

class JsonParser {
public:
    static Json parse(const std::string& text) {
        JsonParser p(text);
        Json v = p.value();
        p.skip_ws();
        if (p.pos_ != p.text_.size()) {
            throw std::runtime_error("trailing JSON content");
        }
        return v;
    }

private:
    explicit JsonParser(const std::string& text) : text_(text) {}

    const std::string& text_;
    std::size_t pos_ = 0;

    [[noreturn]] void fail(const std::string& msg) const {
        throw std::runtime_error("JSON parse error at " +
                                 std::to_string(pos_) + ": " + msg);
    }
    char peek() const {
        if (pos_ >= text_.size()) fail("unexpected end");
        return text_[pos_];
    }
    char take() {
        char c = peek();
        ++pos_;
        return c;
    }
    void skip_ws() {
        while (pos_ < text_.size() &&
               (text_[pos_] == ' ' || text_[pos_] == '\t' ||
                text_[pos_] == '\n' || text_[pos_] == '\r')) {
            ++pos_;
        }
    }
    void expect(char c) {
        if (take() != c) fail(std::string("expected '") + c + "'");
    }
    bool consume_literal(const char* lit) {
        std::size_t n = std::string(lit).size();
        if (text_.compare(pos_, n, lit) == 0) {
            pos_ += n;
            return true;
        }
        return false;
    }

    Json value() {
        skip_ws();
        char c = peek();
        switch (c) {
            case '{': return object();
            case '[': return array();
            case '"': return string_value();
            case 't':
            case 'f':
            case 'n': return literal();
            default: return number();
        }
    }

    Json object() {
        Json v;
        v.type = Json::Type::Obj;
        expect('{');
        skip_ws();
        if (peek() == '}') {
            ++pos_;
            return v;
        }
        for (;;) {
            skip_ws();
            std::string key = parse_string();
            skip_ws();
            expect(':');
            v.obj[key] = value();
            skip_ws();
            char c = take();
            if (c == '}') break;
            if (c != ',') fail("expected ',' or '}'");
        }
        return v;
    }

    Json array() {
        Json v;
        v.type = Json::Type::Arr;
        expect('[');
        skip_ws();
        if (peek() == ']') {
            ++pos_;
            return v;
        }
        for (;;) {
            v.arr.push_back(value());
            skip_ws();
            char c = take();
            if (c == ']') break;
            if (c != ',') fail("expected ',' or ']'");
        }
        return v;
    }

    Json string_value() {
        Json v;
        v.type = Json::Type::Str;
        v.str = parse_string();
        return v;
    }

    std::string parse_string() {
        expect('"');
        std::string out;
        for (;;) {
            char c = take();
            if (c == '"') break;
            if (c == '\\') {
                char e = take();
                switch (e) {
                    case '"': out.push_back('"'); break;
                    case '\\': out.push_back('\\'); break;
                    case '/': out.push_back('/'); break;
                    case 'b': out.push_back('\b'); break;
                    case 'f': out.push_back('\f'); break;
                    case 'n': out.push_back('\n'); break;
                    case 'r': out.push_back('\r'); break;
                    case 't': out.push_back('\t'); break;
                    case 'u': {
                        // Golden files are ASCII; keep it simple.
                        unsigned code = 0;
                        for (int i = 0; i < 4; ++i) {
                            char h = take();
                            code <<= 4;
                            if (h >= '0' && h <= '9') code |= h - '0';
                            else if (h >= 'a' && h <= 'f') code |= h - 'a' + 10;
                            else if (h >= 'A' && h <= 'F') code |= h - 'A' + 10;
                            else fail("bad \\u escape");
                        }
                        if (code > 0x7F) fail("non-ASCII \\u escape unsupported");
                        out.push_back(static_cast<char>(code));
                        break;
                    }
                    default: fail("bad escape");
                }
            } else {
                out.push_back(c);
            }
        }
        return out;
    }

    Json literal() {
        Json v;
        if (consume_literal("true")) {
            v.type = Json::Type::Bool;
            v.boolean = true;
        } else if (consume_literal("false")) {
            v.type = Json::Type::Bool;
            v.boolean = false;
        } else if (consume_literal("null")) {
            v.type = Json::Type::Null;
        } else {
            fail("bad literal");
        }
        return v;
    }

    Json number() {
        std::size_t start = pos_;
        bool neg = false;
        bool integral = true;
        if (peek() == '-') {
            neg = true;
            ++pos_;
        }
        std::uint64_t mag = 0;
        bool overflow = false;
        while (pos_ < text_.size() && text_[pos_] >= '0' &&
               text_[pos_] <= '9') {
            std::uint64_t d = static_cast<std::uint64_t>(text_[pos_] - '0');
            if (mag > (UINT64_MAX - d) / 10) overflow = true;
            mag = mag * 10 + d;
            ++pos_;
        }
        if (pos_ == start + (neg ? 1 : 0)) fail("bad number");
        if (pos_ < text_.size() &&
            (text_[pos_] == '.' || text_[pos_] == 'e' || text_[pos_] == 'E')) {
            integral = false;
            if (text_[pos_] == '.') {
                ++pos_;
                while (pos_ < text_.size() && text_[pos_] >= '0' &&
                       text_[pos_] <= '9') {
                    ++pos_;
                }
            }
            if (pos_ < text_.size() &&
                (text_[pos_] == 'e' || text_[pos_] == 'E')) {
                ++pos_;
                if (pos_ < text_.size() &&
                    (text_[pos_] == '+' || text_[pos_] == '-')) {
                    ++pos_;
                }
                while (pos_ < text_.size() && text_[pos_] >= '0' &&
                       text_[pos_] <= '9') {
                    ++pos_;
                }
            }
        }
        if (integral && overflow) fail("integer overflow");
        Json v;
        v.type = Json::Type::Num;
        v.is_int = integral;
        v.neg = neg;
        v.mag = mag;
        std::string token = text_.substr(start, pos_ - start);
        v.dbl = std::strtod(token.c_str(), nullptr);
        return v;
    }
};

}  // namespace iap_test
