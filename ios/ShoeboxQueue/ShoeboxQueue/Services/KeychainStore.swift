import Foundation
import Observation
import Security

/// Minimal generic-password wrapper for the API key.
enum KeychainStore {
    struct KeychainError: LocalizedError {
        let status: OSStatus

        var errorDescription: String? {
            let text = SecCopyErrorMessageString(status, nil) as String? ?? "unknown"
            return "Keychain error \(status): \(text)"
        }
    }

    static func get(service: String, account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    static func set(_ value: String, service: String, account: String) throws {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let attributes: [String: Any] = [kSecValueData as String: data]
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add[kSecValueData as String] = data
            add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            status = SecItemAdd(add as CFDictionary, nil)
        }
        guard status == errSecSuccess else {
            throw KeychainError(status: status)
        }
    }

    static func delete(service: String, account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        _ = SecItemDelete(query as CFDictionary)
    }
}

/// Base URL (UserDefaults) + API key (Keychain). The Settings tab edits it; everything else asks
/// it for a client.
@MainActor
@Observable
final class AppSettings {
    static let defaultBaseURL = "http://localhost:8080"
    static let baseURLDefaultsKey = "baseURL"
    static let keychainAccount = "api-key"

    private let defaults: UserDefaults
    private let keychainService: String

    var baseURLString: String
    var apiKey: String

    init(defaults: UserDefaults = .standard, keychainService: String = "com.example.ShoeboxQueue", loadFromKeychain: Bool = true) {
        self.defaults = defaults
        self.keychainService = keychainService
        self.baseURLString = defaults.string(forKey: Self.baseURLDefaultsKey) ?? Self.defaultBaseURL
        if loadFromKeychain {
            self.apiKey = KeychainStore.get(service: keychainService, account: Self.keychainAccount) ?? ""
        } else {
            self.apiKey = ""
        }
    }

    /// `nil` until both a valid http(s) URL and a non-empty key are present.
    var config: APIConfig? {
        guard let url = Self.normalizedURL(baseURLString) else {
            return nil
        }
        let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else {
            return nil
        }
        return APIConfig(baseURL: url, apiKey: key)
    }

    var isConfigured: Bool {
        config != nil
    }

    func save() throws {
        baseURLString = baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        apiKey = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        defaults.set(baseURLString, forKey: Self.baseURLDefaultsKey)
        if apiKey.isEmpty {
            KeychainStore.delete(service: keychainService, account: Self.keychainAccount)
        } else {
            try KeychainStore.set(apiKey, service: keychainService, account: Self.keychainAccount)
        }
    }

    func makeClient() -> APIClient? {
        guard let config else {
            return nil
        }
        return APIClient(config: config)
    }

    /// A client for `/health`, which needs no key; `nil` when the URL is not usable.
    func makeUnauthenticatedClient() -> APIClient? {
        guard let url = Self.normalizedURL(baseURLString) else {
            return nil
        }
        return APIClient(config: APIConfig(baseURL: url, apiKey: apiKey.trimmingCharacters(in: .whitespacesAndNewlines)))
    }

    static func normalizedURL(_ text: String) -> URL? {
        var trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        while trimmed.hasSuffix("/") {
            trimmed.removeLast()
        }
        guard !trimmed.isEmpty, let url = URL(string: trimmed), let scheme = url.scheme?.lowercased() else {
            return nil
        }
        guard scheme == "http" || scheme == "https", let host = url.host, !host.isEmpty else {
            return nil
        }
        return url
    }
}
