import { useMDXComponents as getMDXComponents } from 'nextra-theme-docs'

export function useMDXComponents(components) {
  return getMDXComponents({
    ...components,
  })
}
