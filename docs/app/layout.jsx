import { Footer, Layout, Navbar } from 'nextra-theme-docs'
import { Head } from 'nextra/components'
import { getPageMap } from 'nextra/page-map'
import 'nextra-theme-docs/style.css'

export const metadata = {
  title: {
    default: 'Shoebox',
    template: '%s | Shoebox',
  },
  description: 'Automation toolkit for running an eBay sports-card store',
}

const navbar = <Navbar logo={<b>Shoebox</b>} projectLink="https://github.com/coperyan/shoebox" />
const footer = <Footer>MIT {new Date().getFullYear()} &copy; Ryan Cope.</Footer>

export default async function RootLayout({ children }) {
  return (
    <html lang="en" dir="ltr" suppressHydrationWarning>
      <Head />
      <body>
        <Layout
          navbar={navbar}
          pageMap={await getPageMap('/')}
          docsRepositoryBase="https://github.com/coperyan/shoebox/tree/main/docs/content"
          footer={footer}
          sidebar={{ defaultMenuCollapseLevel: 1 }}
        >
          {children}
        </Layout>
      </body>
    </html>
  )
}
